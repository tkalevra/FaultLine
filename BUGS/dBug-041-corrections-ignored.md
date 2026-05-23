# dBug-041: Ingestion of Corrected Facts Are Being Ignored

**Status**: OPEN  
**Severity**: HIGH  
**Affected Component**: Ingest Pipeline (src/api/main.py)  
**Date Reported**: 2026-05-17

## Summary

When users attempt to correct facts (e.g., "Actually, I am 30 years old, not 25"), the corrected facts are extracted by the LLM but **rejected during ingest validation**, preventing the correction from being applied to the knowledge graph.

The user receives LLM acknowledgment ("Got it! Thanks for correcting me") but the facts are silently dropped, creating a false sense that corrections were applied.

## Root Cause Analysis

**Investigation Date**: 2026-05-17 04:45:00 UTC  
**Test Input**: "Actually, I am 30 years old, not 25"

### Pipeline Flow - Where It Breaks

1. ✅ **Query Phase**: `/query` recognized "30" as an entity, returned context
2. ✅ **Extraction Phase**: LLM extracted `(user, age, "30")`
3. ❌ **Ingest Phase**: Fact rejected with two failures:

#### Failure Point 1: Entity Name Validation
```
2026-05-17 04:45:03 [warning] ingest.entity_name_rejected
  reason=entity_name_invalid_pure_numeric
  rel_type=pref_name
  subject=30
```

**Issue**: The number "30" is in the entity blocklist as a pure numeric value (`ENTITY_NAME_BLOCKLIST` in main.py line 56-75). When LLM extracts "30" as an entity (for pref_name registration), the entity registry rejects it.

#### Failure Point 2: Age Subject Validation
```
2026-05-17 04:45:03 [warning] ingest.age_rejected_wrong_subject
  object=30
  subject=user
  text='Actually, I am 30 years old, not 25'
```

**Issue**: The age fact was rejected because the subject was "user" (not a specific entity UUID). The validation logic appears to require specific entity types for age facts, not the general "user" identity.

### Why Corrections Fail

1. **User input**: "Actually, I am 30 years old, not 25"
2. **LLM extraction**: `(user, age, "30"), (user, age, "25")`
3. **Expected ingest**: Both facts ingested, old fact superseded, new fact committed
4. **Actual ingest**: 
   - Age facts rejected (wrong subject or validation failed)
   - No supersession occurs
   - Old fact remains untouched

## Evidence

**Log Analysis** (full logs appended below):

```
query.entity_resolution.resolved
  resolved=[('c06f5b14-a011-56c4-9c57-d2c30731be79', 'old'), 
           ('40630927-41ff-57fc-b15c-39760738bd8a', 'years'),
           ('c50d8a39-ab1c-55e4-9f77-1aa3baa02709', 'not'),
           ('7cbbf07f-4a43-5842-95b3-b0e8ce614b59', 'actually')]

ingest.age_rejected_wrong_subject
  [CRITICAL] Age fact rejected during ingest

ingest.entity_name_rejected
  reason=entity_name_invalid_pure_numeric
  [CRITICAL] Pure numeric blocklist blocks "30"
```

## Affected Workflows

1. **User corrections** ("Actually, I meant..." , "That was wrong...")
2. **Attribute updates** ("I'm now 30", "My new address is...")
3. **Fact supersession** (old fact should be archived, new fact committed)

## Related Architecture

**Retraction/Correction Handling** (CLAUDE.md):
```
Inlet Short-Circuit & Retraction (lines 1114-1141):
  IF text contains retraction signals ("forget", "delete", "wrong", etc.)
    → LLM extracts {subject, rel_type?, old_value?}
    → POST /retract → database supersession
    → confirmation system message → inlet returns early

Extraction marks corrections: is_correction=true (faultline_function.py line 478)
```

But the `/ingest` endpoint does NOT respect `is_correction` flag when processing corrected facts.

### **CRITICAL FINDING**: is_correction Flag Lost in Transit

**Evidence** (faultline_function.py):

```python
Line 478: edge["is_correction"] = True  # ✅ Flag is set on edges

Line 1596: await self._fire_ingest(clean_text, ..., edges=None)  # ❌ edges=None!
```

**The Problem**:
1. Filter detects correction signal ("Actually", "not", "wrong") → sets `is_correction=True`
2. BUT Filter explicitly sends `edges=None` to ingest
3. Comment says: "Filter is dumb — send ONLY raw text, NO edges"
4. Result: The correction context is **completely discarded**
5. Ingest sees plain text "Actually, I am 30 years old, not 25" with NO indication it's a correction
6. Ingest applies regular validation → fails → fact rejected

**Impact**: The confidence filter (MIN_INJECT_CONFIDENCE valve) is implemented for query injection but **NOT implemented for correction ingestion**. User corrections bypass the confidence gate entirely and fail validation.

## Expected Behavior

1. User sends: "Actually, I am 30 years old, not 25"
2. Filter detects correction signal ("Actually", "not", "am")
3. LLM extracts: `(user, age, "30")` with `is_correction=true`
4. Ingest receives marked-as-correction fact
5. **Ingest should**:
   - Accept correction facts with less strict validation (or bypass blocklist for corrections)
   - Supersede old `(user, age, "25")` fact
   - Commit new `(user, age, "30")` as Class A (user-stated correction)
6. Query reflects updated age in subsequent calls

## Current Behavior

1-3. ✅ Works as expected  
4-6. ❌ Fact silently rejected, old value persists

## Test Case

```bash
# Test correction injection
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-1cf72f713e884a06b3dab80a8a003669" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [
      {"role": "user", "content": "Actually, I am 30 years old, not 25"}
    ],
    "stream": false
  }'

# Expected: LLM response + age updated to 30 in knowledge graph
# Actual: LLM response received, but age remains unchanged in database
```

## Files Affected

- `openwebui/faultline_function.py` (line 478: `is_correction` flag set)
- `src/api/main.py` (line 2100-2300: ingest pipeline doesn't handle corrections)
- `src/api/main.py` (line 54-75: `ENTITY_NAME_BLOCKLIST` blocks pure numerics)

## Fix Strategy (Pending)

### **Root Problem**: Correction context lost between Filter and Ingest

The `is_correction=True` flag is set in Filter but never reaches Ingest because `edges=None`.

### **Solution Options**

1. **Option A** (RECOMMENDED): Pass correction signal to ingest via source/metadata
   - Modify `_fire_ingest()` signature to accept `is_correction` parameter
   - Change line 1596 from `edges=None` to `is_correction=True` when correction detected
   - Ingest receives flag and applies high-confidence routing (bypass blocklist, Class A directly)
   - Keep architecture (Filter dumb, Ingest strong) while preserving correction context

2. **Option B**: Detect correction signals in Ingest independently
   - Don't rely on Filter to mark corrections
   - Have Ingest check for correction keywords ("Actually", "not", "wrong", "incorrect")
   - Apply same confidence gate when detected
   - Redundant but safer (no context loss possible)

3. **Option C**: Bypass validation entirely for user-stated facts
   - Trust that if user explicitly corrects, it's high-confidence (Class A)
   - Don't validate entity names/types for facts marked as corrections
   - Use `/retract` endpoint flow for true supersession guarantee

**CRITICAL**: Without passing `is_correction` to ingest, the confidence filter that was implemented for query injection is **completely bypassed** for corrections. This is the architectural flaw.

**Recommended**: Option A (minimal change, preserves architecture, fixes context loss)

## Investigation Commands

```bash
# Check FaultLine logs for rejection messages
ssh docker-host -x "sudo docker logs faultline 2>&1 | grep -E 'rejected|correction|age|subject'"

# Check database for presence of corrected facts
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"SELECT * FROM facts WHERE rel_type='age' AND user_id='${TEST_USER_ID}' ORDER BY created_at DESC LIMIT 5;\""

# Check staged_facts for pending corrections
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"SELECT * FROM staged_facts WHERE rel_type='age' ORDER BY created_at DESC LIMIT 5;\""
```

## Full Logs

```
2026-05-17 04:45:00 [info     ] query.entity_resolution.start  has_attribute_signal=True query_lower='actually, i am 30 years old, not 25'
2026-05-17 04:45:00 [info     ] query.entity_resolution.tokens tokens=['actually', 'am', '30', 'years', 'old', 'not', '25']
2026-05-17 04:45:00 [info     ] query.entity_resolution.resolved resolved=[('c06f5b14-a011-56c4-9c57-d2c30731be79', 'old'), ('40630927-41ff-57fc-b15c-39760738bd8a', 'years'), ('c50d8a39-ab1c-55e4-9f77-1aa3baa02709', 'not'), ('7cbbf07f-4a43-5842-95b3-b0e8ce614b59', 'actually')]
2026-05-17 04:45:03 [warning  ] ingest.age_rejected_wrong_subject object=30 subject=user text='Actually, I am 30 years old, not 25'
2026-05-17 04:45:03 [warning  ] ingest.entity_name_rejected reason=entity_name_invalid_pure_numeric rel_type=pref_name subject=30
```

---

**Next Steps**: Await confirmation to implement fix (Phase 1-3 of restoration).
