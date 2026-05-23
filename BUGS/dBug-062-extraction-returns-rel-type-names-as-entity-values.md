# dBug-062: LLM Extraction Returns Rel_Type Names as Entity Values

**Status:** OPEN  
**Severity:** CRITICAL — Data corruption, entity deduplication failures  
**Date Found:** 2026-05-21  
**Reporter:** Claude Code  
**Affects:** `/extract/rewrite` endpoint, LLM triple generation  

---

## Summary

The `/extract/rewrite` LLM endpoint returns malformed triples where **rel_type names are substituted for entity values**. This causes:

1. Invalid facts with rel_type names as objects (e.g., `charlie pref_name pref_name`)
2. Missing entity deduplication (no "${ENTITY}" as alias for "charlie" because extraction returns `charlie pref_name pref_name`)
3. Scalar attributes with rel_type values instead of real values (e.g., `charlie age age` instead of `charlie age 19`)
4. Entity creation for rel_type names as separate entities (e.g., "parent_of" becomes a child entity)

---

## Evidence

### Test Case
**Input:** "My son charlie is also known as ${ENTITY}, he is 19 and an ${ENTITY} Major at University. He enjoys ${ENTITY} and crafts."

**Expected Extractions:**
- `charlie pref_name charlie`
- `charlie also_known_as ${ENTITY}`
- `charlie age 19`
- `charlie occupation ${ENTITY} Major`
- `charlie likes ${ENTITY}` (concept)
- `user parent_of charlie`

**Actual Extractions (from logs):**

```json
{
  "subject": "charlie",
  "object": "pref_name",    // ❌ WRONG: rel_type name, not entity value
  "rel_type": "pref_name",
  "definition": "charlie is also known as ${ENTITY}."
}
```

And:
```
[warning] ingest.age_rejected_non_numeric_object
  object=age                 // ❌ WRONG: extracted "age" as value, not "19"
  subject=charlie
```

And:
```
[info] ingest.pref_name_injected
  entity=also_known_as       // ❌ WRONG: "also_known_as" treated as entity name
```

And:
```
[info] ingest.subject_resolved_at_extraction
  input=pref_name output=55c13545-3f9a-5798-8827-c35e7c9cfa70
```

### Database Impact

From query logs, the system shows:
```
entity_attrs={'55c13545-3f9a-5798-8827-c35e7c9cfa70': {
  'pref_name': {'value': 'pref_name'},      // ❌ Should be "charlie" or "${ENTITY}"
  'also_known_as': {'value': '${ENTITY}'},         // ✓ Correct
}}
```

And filter injection shows:
```
[FaultLine Filter] fact: pref_name -occupation-> ${ENTITY} major
[FaultLine Filter] fact: pref_name -also_known_as-> ${ENTITY}
```

**"pref_name" is being treated as an entity.**

---

## Root Cause Analysis

### Hypothesis 1: Extraction Prompt Inclualice Rel_Type Names

The `/extract/rewrite` system prompt likely inclualice rel_type names in:
- List of valid rel_types
- Example relationships
- Schema documentation

The LLM may be:
1. Reading these rel_type names in the prompt
2. Confusing them with valid entity names
3. Hallucinating them as actual triple values instead of relationship labels

**Location to investigate:** `src/api/main.py` lines 3090-3125 (_build_extraction_prompt)

### Hypothesis 2: Schema/Constraint Injection

If `typed_entities` are injected into the extraction prompt as examples (similar to dBug-021), the LLM may be confusing:
- Rel_type names in the schema
- Entity names in the examples
- Valid value placeholders

---

## Impact Assessment

### Data Corruption

| What Should Happen | What Actually Happens |
|---|---|
| `charlie also_known_as ${ENTITY}` (entity alias deduplication) | `charlie pref_name pref_name` (invalid fact, no deduplication) |
| `charlie age 19` (scalar attribute) | `charlie age age` (invalid, rejected as non-numeric) |
| `user parent_of charlie` (relationship) | `user parent_of ${ENTITY}` (wrong entity reference) |

### System Failures

1. **Entity Deduplication Broken:** "charlie" and "${ENTITY}" never merge as aliases because extraction fails to produce the `also_known_as` edge
2. **False Entity Creation:** "pref_name", "also_known_as", "parent_of" become entities in the database
3. **Scalar Validation Failures:** Age/occupation facts rejected because values are rel_type names, not data
4. **Memory Injection Corruption:** Filter shows entity names like "Parent_Of" and "pref_name" in family relationships

---

## Investigation Steps

### 1. Examine Extraction Prompt Template

**File:** `src/api/main.py:3090-3125` (_build_extraction_prompt)

```bash
# Check if rel_type names appear in system prompt
grep -n "pref_name\|also_known_as\|parent_of" /home/${USER}/Documents/013-GIT/FaultLine-dev/src/api/main.py | head -50
```

**Question:** Does the prompt accidentally include rel_type names where examples should show entity names?

### 2. Inspect LLM Response Before Validation

**Location:** `src/api/main.py:3247-3258` (after response.json())

Add logging to capture raw LLM output BEFORE `_validate_triple_against_metadata()` processes it:

```python
log.debug("extract_rewrite.raw_llm_response", content=content, triples=triples)
```

This will show what the LLM actually returned before any processing.

### 3. Check typed_entities Injection

**Location:** `src/api/main.py:3182-3190`

If `typed_entities` are being injected as examples, verify they're not confusing the LLM about what constitutes a valid entity vs rel_type.

### 4. GLiNER2 Output

**Location:** `src/api/main.py:3631-3656`

What does GLiNER2 return? Are rel_type names appearing in its entity extraction?

```bash
# Check GLiNER2 logs for what entities it detected
ssh docker-host -x "sudo docker logs faultline 2>&1" | grep gliner2
```

---

## Reproduction

**Message to test:**
```
My son charlie is also known as ${ENTITY}. He is 19 and an ${ENTITY} Major. ${ENTITY} enjoys ${ENTITY} and crafts.
```

**Expected behavior:**
- Facts about "charlie" with correct attributes (age=19, occupation=${ENTITY} Major)
- "${ENTITY}" registered as alias for charlie
- No rel_type names appearing as entities

**Current behavior:**
- Facts contain rel_type names as values (pref_name, also_known_as, age, parent_of)
- Multiple entities created for "charlie", "${ENTITY}", "pref_name", "parent_of"
- False parent_of relationships (user parent_of ${ENTITY}, not user parent_of charlie)
- Query injection shows "Parent_Of" and "pref_name" as children names

---

## Related Issues

- **dBug-${ENTITY}-false-children:** ${ENTITY} false entity — ROOT CAUSE appears to be this extraction bug, not just ingest ordering
- **dBug-021:** Hardcoded regex workarounds for extraction failures — symptom of extraction issues
- **dprompt-126 Layer 1:** Hierarchy validation can't prevent this because false entities are created with correct types due to extraction confusion

---

## Solution Requirements

The fix must ensure:
1. Extraction returns **only valid entity names**, never rel_type names
2. Scalar rel_types (age, pref_name, occupation) get **data values**, not rel_type names  
3. Identity edges (also_known_as, pref_name) get **entity aliases**, not rel_type names
4. No rel_type names appear in the extraction prompt in contexts where the LLM might confuse them with entity examples

**Implementation:** Clarify extraction prompt or fix LLM output validation to reject and log triples with rel_type names as entity values.

---

## Testing After Fix

```bash
# Test message
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [
      {
        "role": "user",
        "content": "My son charlie is also known as ${ENTITY}, he is 19 and an ${ENTITY} Major at University."
      }
    ]
  }'

# Verify facts via follow-up query
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [
      {
        "role": "user",
        "content": "Tell me about charlie."
      }
    ]
  }'

# Should recall: charlie is your son, age 19, ${ENTITY} Major, also known as ${ENTITY}
```

---

## Blockers

- Cannot easily see raw LLM extraction output without modifying logging
- Need to check OpenWebUI LLM response before FaultLine processing
- Needs detailed extraction prompt audit

---

## Notes

This bug was discovered while testing the fix for dBug-${ENTITY}-false-children after removing hardcoded `_infer_type_from_relationship()` function. The metadata-driven type validation is now working correctly, but extraction is producing invalid triples in the first place.

The fact that `charlie` resolves correctly to UUID `55c13545-3f9a-5798-8827-c35e7c9cfa70` but gets stored with attributes like `pref_name='pref_name'` and `also_known_as='${ENTITY}'` shows the problem is specifically in what LLM returns for rel_type objects, not in entity resolution.
