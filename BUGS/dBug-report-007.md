# dBug-report-007: Bidirectional Validation Failed + UUID Exposure in Response

**Date:** 2026-05-13  
**Severity:** P1 (Data integrity + response accuracy)  
**Status:** Investigation complete — dprompt-62 validation incomplete  

## Symptom

Post-dprompt-62 deployment shows:

1. **Impossible bidirectional relationship still in database:**
   ```
   user -child_of-> gabby (conf=1) ← User is child of daughter (IMPOSSIBLE)
   user -parent_of-> des (conf=1)
   user -parent_of-> cyrus (conf=1)
   ```
   User cannot be both child_of Gabby AND parent_of Des/Cyrus.

2. **UUID exposure in query response:**
   ```
   - 7E4Bff75-706E-5Feb-B8B5-F4Ca1247Fd3B is species: morkie mix
   - 550Fc016-E544-5Ec9-9Fb7-1Cbb86757Deb is 19 years old
   ```
   Should display: "Fraggle is species: morkie mix", "Cyrus is 19 years old"

## Investigation Findings

### Database State (Post-dprompt-62)

**Impossible relationship preserved:**
```sql
SELECT subject_id, rel_type, object_id, confidence 
FROM facts 
WHERE subject_id = '3f8e6836-72e3-43d4-bbc5-71fc8668b070'
AND rel_type IN ('child_of', 'parent_of');

Result:
3f8e6836... child_of d4bf6c7b... (gabby) conf=1 ← IMPOSSIBLE
3f8e6836... parent_of 0638cc40... (des) conf=1
3f8e6836... parent_of 550fc016... (cyrus) conf=1
```

**Status:** Fact still exists, NOT superseded. Confidence=1 (highest), suggesting it was inferred or user-stated.

### OpenWebUI Logs

UUID exposure pattern:
```
- 7E4Bff75-706E-5Feb-B8B5-F4Ca1247Fd3B is species: morkie mix
- 550Fc016-E544-5Ec9-9Fb7-1Cbb86757Deb is 19 years old
- 0638Cc40-B16D-575C-8873-E1158Cc3C27C is 12 years old
- D4Bf6C7B-A9Ab-5D1C-8612-54D47Fd90Bd7 is 10 years old
```

Facts are being returned from `/query` with UUIDs instead of display names. Filter is injecting them as-is.

### Root Causes

**Issue 1: Bidirectional Validation Failed**
- dprompt-62 `_validate_bidirectional_relationships()` NOT catching child_of + parent_of coexistence
- Possible causes:
  a. Validation logic has bug (not checking inverse correctly)
  b. Logic inverted (catching non-issues, missing actual issues)
  c. Validation not being called for this pattern
  d. Relationship created BEFORE dprompt-62 deployed (already in database)

**Issue 2: UUID in Response**
- `/query` returning facts with UUID as subject_id/object_id
- Filter injecting them directly without resolving to display names
- Response builder (_build_entity_types()? _resolve_display_names()?) not converting UUIDs to aliases

**Issue 3: dprompt-62 Deployment Status Unclear**
- Logs show ingest firing with status 200 OK
- But validation isn't catching impossible relationship
- Need to verify: Is dprompt-62 code actually running?

## Test Case

**Input:** "Tell me about my family"

**Current output (WRONG):**
```
Facts returned include:
- 550Fc016-E544-5Ec9-9Fb7-1Cbb86757Deb is 19 years old (UUID, should be "Cyrus is 19 years old")
- user -child_of-> gabby (impossible, should not exist)
```

**Expected output:**
```
Facts returned:
- cyrus is 19 years old (display name)
- user -parent_of-> gabby (one direction only, no child_of)
- No impossible bidirectional relationships
```

## Affected Components

1. **dprompt-62 validation** — bidirectional check not catching child_of + parent_of coexistence
2. **Query response building** — UUIDs not being resolved to display names
3. **Filter injection** — injecting UUID facts directly instead of display names

## Next Steps

### Immediate (Database Cleanup)

**Delete impossible child_of fact:**
```sql
DELETE FROM facts
WHERE subject_id = '3f8e6836-72e3-43d4-bbc5-71fc8668b070'
AND rel_type = 'child_of'
AND object_id = 'd4bf6c7b-a9ab-5d1c-8612-54d47fd90bd7'
AND confidence = 1;
```

After cleanup, query should return only:
- `user parent_of des` (conf=1)
- `user parent_of cyrus` (conf=1)
- `user parent_of gabby` (inferred from gabby child_of user)

### Short Term (Code Fixes)

**dprompt-63:** Fix bidirectional validation logic
- Review `_validate_bidirectional_relationships()` in dprompt-62 implementation
- Verify inverse rel_type detection working correctly
- Add explicit logging to trace why child_of + parent_of not caught

**dprompt-64:** UUID resolution in query response
- Fix `/query` response building to resolve UUIDs to display names
- Verify `_resolve_display_names()` is being called
- Check if dprompt-61 deduplication affected UUID resolution

## References

- dBug-report-006.md — Previous bidirectional investigation
- dprompt-62.md/62b.md — Bidirectional validation spec (incomplete)
- OpenWebUI logs — UUID exposure evident in injected facts
- CLAUDE.md — UUID exposure constraint, display name semantics

---

**Status:** Bidirectional validation incomplete + UUID resolution broken. Recommend:
1. Clean database now (remove impossible child_of)
2. Debug dprompt-62 bidirectional logic
3. Fix UUID resolution in query response building
