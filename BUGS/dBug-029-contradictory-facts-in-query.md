# dBug-029: Contradictory & Malformed Facts in /query Response

**Status:** OPEN  
**Severity:** CRITICAL (LLM cannot resolve facts)  
**Date Reported:** 2026-05-16 01:15 UTC  
**Impact:** Query returns conflicting facts that confuse LLM; family data corrupted

---

## Broken Behavior

**Query:** `/query` for user `${TEST_USER_ID}`, text "tell me about my family"

**Returns 45 facts with CRITICAL ISSUES:**

### Issue 1: Contradictory Facts About ${ENTITY}
```
Fact 3:  ${ENTITY} instance_of pet        ✓
Fact 4:  ${ENTITY} not_instance_of pet    ✗ (contradicts above)
```
**Problem:** Both facts returned. LLM cannot resolve: is ${ENTITY} a pet or not?

### Issue 2: User Entity Identity Fragmented
```
Fact 1:   user pref_name ca
Subject references: ${USER}, we, ca, user, d414434d-...
```
**Problem:** 
- Facts reference user as "${USER}", "we", "user", and UUID
- No unified entity anchor
- LLM sees disconnected family members

### Issue 3: Pronoun Pollution
```
Fact 6: we parent_of alice      ✗ (pronoun should be cleaned)
Fact 7: alice child_of we       ✗ (pronoun in fact)
```
**Problem:** 
- "we" appears as subject (pronoun should have been rejected by dBug-025 validation)
- Should reference "${USER}" or user UUID, not "we"

### Issue 4: Missing Children
**Expected:** charlie, bob, alice (3 children)  
**Returned:** Only alice (1 child)

**Problem:** 
- charlie and bob child_of relationships missing from response
- Query deduplication may be filtering them incorrectly
- Or facts not in database

---

## Root Causes

### 1. Contradictory Facts in PostgreSQL
- Both `${ENTITY} instance_of pet` AND `${ENTITY} not_instance_of pet` stored
- User correction ("${ENTITY} is a computer") created `not_instance_of` but didn't supersede original
- Both returned by query → LLM confused

### 2. User Entity Not Normalized
- Child facts reference "we" (pronoun from failed extraction)
- Parent facts reference "${USER}" (display name)
- User identity fact references "ca" (alias)
- Query returns all variants without consolidation

### 3. Query Deduplication Not Working
- Expected: Deduplicate on (subject_uuid, rel_type, object_uuid)
- Actual: Returning both contradictory facts
- charlie/bob missing entirely (possible dedup failure)

### 4. Pronoun Cleanup Not Applied
- dBug-025 should reject "we" at write-time
- Old facts with "we" subject still in database
- Query returns pronoun facts without filtering

---

## Data State

**PostgreSQL facts for user `${TEST_USER_ID}`:**
```
Total: 45 returned by /query
Issues:
- ${ENTITY} instance_of pet (old)
- ${ENTITY} not_instance_of pet (new correction)
- we parent_of alice (pronoun, should be deleted)
- alice child_of we (pronoun, should be deleted)
- charlie/bob child relationships missing
```

---

## Expected Behavior

**Query should return:**
1. **No contradictions** — supersede old "instance_of pet" with new "not_instance_of pet" or delete old
2. **Normalized user identity** — one fact per relationship using UUID or primary alias
3. **No pronouns** — filter out "we" facts entirely
4. **All children** — charlie, bob, alice all returned
5. **Deduplicated** — one fact per (subject, rel_type, object) triple

---

## Test Results (2026-05-16 01:15 UTC)

**Test 1: "tell me about my family"**
```json
[
  {"subject": "user", "rel_type": "pref_name", "object": "ca"},
  {"subject": "${ENTITY}", "rel_type": "instance_of", "object": "pet"},
  {"subject": "${ENTITY}", "rel_type": "not_instance_of", "object": "pet"},  // CONFLICT
  {"subject": "${USER}", "rel_type": "has_pet", "object": "${ENTITY}"},
  {"subject": "we", "rel_type": "parent_of", "object": "alice"},            // PRONOUN
  {"subject": "alice", "rel_type": "child_of", "object": "we"}              // PRONOUN
]
```

**Test 2: "who is my spouse"**
- /query returns: `${USER} spouse emma` ✓
- LLM response: "No spouse record found" ✗
- Status: Fact available but LLM not using it

---

## For DEEPSEEK

**Investigate:**

1. **Contradictory facts in /query response**
   - Why are both `instance_of pet` and `not_instance_of pet` returned?
   - Should user correction supersede old fact? (Check dBug-025 retraction flow)
   - Should query deduplicate contradictions by keeping highest confidence?

2. **Pronoun pollution in query results**
   - "we" facts should have been cleaned by dBug-025
   - Are old "we" facts still in PostgreSQL?
   - Why isn't query filtering them out?

3. **Missing children (charlie, bob)**
   - Are child_of facts in PostgreSQL? (Check `child_of charlie`, `child_of bob`)
   - If yes: Why doesn't /query return them?
   - If no: Were they deleted? When?

4. **User entity identity fragmentation**
   - Facts reference: "user", "${USER}", "we", "ca", UUID
   - Should /query normalize to one representation?
   - Check: `_resolve_display_names()` logic

**Deliverables:**
1. Verify fact state in PostgreSQL (run queries in dBug-029 database check section)
2. Identify which issue causes which symptom
3. Report findings in scratch with specific SQL results + log lines

---

## Verification SQL

Run these in pre-prod to diagnose:

```sql
-- Check contradictory ${ENTITY} facts
SELECT subject_id, object_id, rel_type, confidence, superseded_at 
FROM facts 
WHERE user_id = '${TEST_USER_ID}' 
AND (subject_id LIKE '%${ENTITY}%' OR object_id LIKE '%${ENTITY}%')
ORDER BY created_at DESC;

-- Check for "we" pronoun facts (should be none)
SELECT subject_id, object_id, rel_type 
FROM facts 
WHERE user_id = '${TEST_USER_ID}' 
AND subject_id = 'we';

-- Check children (should find charlie, bob, alice)
SELECT subject_id, object_id, rel_type 
FROM facts 
WHERE user_id = '${TEST_USER_ID}' 
AND (rel_type = 'child_of' OR rel_type = 'parent_of')
ORDER BY subject_id, object_id;
```
