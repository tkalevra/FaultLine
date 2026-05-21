# dBug-042: Retraction Signal Detection Missing Negation Patterns

**Status**: ✅ FIXED  
**Severity**: HIGH  
**Affected Component**: OpenWebUI Filter (openwebui/faultline_function.py) → Backend retraction_signals table  
**Date Reported**: 2026-05-17  
**Fixed**: 2026-05-18 via dprompt-105/107 (negation patterns + categorical retraction)  
**Production Deployed**: 2026-05-18 commit 0437f1a  
**Solution**: Pattern metadata (retraction_signals table) now inclualice negation patterns. Backend dprompt-115 unified gate matches against learned patterns

## Summary

When users attempt to remove facts using negation patterns (e.g., "Bob is not my child", "X isn't my Y"), the retraction detection system **fails to recognize these as retraction signals**, causing the correction to be ignored and the unwanted fact to persist in the knowledge graph.

The system currently only matches explicit retraction keywords like "forget", "delete", "wrong", but NOT implicit negations like "is not", "isn't", "am not", "aren't".

## Root Cause Analysis

**Investigation Date**: 2026-05-17 15:24:00 UTC  
**Test Input**: "Bob is not my child"

### The Problem

The retraction signal detection uses a hardcoded frozenset of keywords:

```python
# openwebui/faultline_function.py (line ~550)
_RETRACTION_SIGNALS: frozenset[str] = frozenset({
    "forget", "delete", "remove", "retract", "erase",
    "that's wrong", "thats wrong", "that was wrong", "not true",
    "that's not right", "thats not right", "incorrect", "no longer",
    "remove from memory", "forget that", "don't remember",
    "that information is wrong", "that info is wrong",
})
```

**Missing patterns that users naturally use to retract facts:**
- "is not" / "isn't" / "am not" / "aren't"
- "not my" / "not a"
- "wrong about"
- "no, that's"
- "actually, it's"
- "I meant to say"
- "scratch that"

### Evidence

**Test Case**: User message "Bob is not my child"

1. ✅ Message received by Filter inlet
2. ❌ `_detect_retraction_intent()` checks if message contains any signal
3. ❌ "is not" pattern NOT in `_RETRACTION_SIGNALS`
4. ❌ Retraction detected = False → `_extract_retraction()` never called
5. ❌ `/retract` endpoint never invoked
6. ❌ Fact `(user, parent_of, bob)` persists in database with `superseded_at = NULL`
7. ❌ User receives LLM response saying "I cannot update or remove facts" (false — system can, it just didn't try)

**Database Verification**:
```
facts table:
  subject_id = user_uuid
  rel_type = parent_of / child_of
  object_id = bob_uuid
  superseded_at = NULL  ← Should be NOW() if retraction worked
```

### Call Chain Breakdown

```
inlet() message="Bob is not my child"
  ↓
_detect_retraction_intent()
  ↓ 
Check: "is not" in _RETRACTION_SIGNALS?
  ↓ NO
  ↓
Return: False (not a retraction)
  ↓
Skip _extract_retraction()  ← BUG: Should have called this
  ↓
Skip _fire_retract()  ← BUG: Should have POSTed to /retract
  ↓
Proceed to normal ingest flow (wrong!)
  ↓
LLM response: "I cannot update..."  ← Misleading
```

## Expected Behavior

When user says "Bob is not my child":

1. Filter inlet detects "is not" as retraction signal
2. Calls `_extract_retraction(text, context, model, ...)`
3. LLM extracts: `{"subject": "user", "rel_type": "parent_of", "old_value": "bob"}`
4. Calls `_fire_retract()` → POST `/retract` endpoint
5. Backend supersealice fact: `UPDATE facts SET superseded_at = now() WHERE (user, parent_of, bob)`
6. Cache busted
7. Filter returns early with confirmation: "Got it, I've removed Bob from your family."
8. No ingest happens (short-circuit)
9. Query reflects updated relationships

## Current Behavior

1-7. ❌ Retraction detection fails
8. Instead proceeds to ingest (wrong!)
9. LLM extracts fact but ingest may reject it
10. User receives confusing message: "I cannot update or remove facts directly"

## Test Case

```bash
# Test 1: Direct negation (FAILS)
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-1cf72f713e884a06b3dab80a8a003669" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [
      {"role": "user", "content": "Bob is not my child"}
    ],
    "stream": false
  }'

# Expected: "Got it, I've removed Bob from your family."
# Actual: "I cannot update or remove facts directly, but I've noted the correction..."

# Test 2: Verify fact NOT superseded (confirms bug)
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"SELECT rel_type, object_id, superseded_at FROM facts 
   WHERE rel_type IN ('parent_of', 'child_of') 
   AND object_id = (SELECT entity_id FROM entity_aliases WHERE alias='bob')
   AND superseded_at IS NULL;\""

# Expected: (no rows)
# Actual: Returns parent_of and child_of facts with superseded_at = NULL (not removed)
```

## Affected Workflows

1. **Direct negation removals** ("X is not my Y", "X isn't my Y")
2. **Implicit corrections** ("I was wrong about X", "Actually, X is Y not Z")
3. **Identity corrections** ("No, I'm not Y", "That's not me")
4. **Relationship removals** ("Bob isn't a family member", "alice isn't my son anymore")
5. **Attribute corrections** ("That's not my address", "Wrong phone number")

## Files Affected

- `openwebui/faultline_function.py` (line ~550: `_RETRACTION_SIGNALS` frozenset)
- `openwebui/faultline_function.py` (line ~600: `_detect_retraction_intent()` function)

## Fix Strategy

### Root Problem
`_RETRACTION_SIGNALS` frozenset is incomplete. It only covers explicit retraction keywords, not implicit negations.

### Solution (RECOMMENDED)
Expand `_RETRACTION_SIGNALS` to include common negation and correction patterns:

```python
_RETRACTION_SIGNALS: frozenset[str] = frozenset({
    # Explicit retractions (existing)
    "forget", "delete", "remove", "retract", "erase",
    "that's wrong", "thats wrong", "that was wrong", "not true",
    "that's not right", "thats not right", "incorrect", "no longer",
    "remove from memory", "forget that", "don't remember",
    "that information is wrong", "that info is wrong",
    
    # NEW: Implicit negations and corrections
    "is not", "isn't", "am not", "aren't", "wasn't", "weren't",
    "is not a", "isn't a", "not my", "not my",
    "wrong about", "wrong about",
    "i was wrong", "i'm wrong", "that's wrong",
    "no, that", "nope, that",
    "scratch that", "actually,",
    "i meant", "what i meant",
    "should be", "should have been",
})
```

### Implementation Steps

1. **Add negation patterns** to `_RETRACTION_SIGNALS` (7-10 new patterns)
2. **Test with common negations**:
   - "X is not my Y" → Detects retraction ✓
   - "X isn't a Y" → Detects retraction ✓
   - "I was wrong about X" → Detects retraction ✓
3. **Verify retraction flow**:
   - `_detect_retraction_intent()` returns True
   - `_extract_retraction()` is called
   - `/retract` endpoint called
   - Fact is superseded in database
   - User receives confirmation message

### Risk Assessment
- **Low Risk**: Only adding more keywords to detection
- **No Breaking Changes**: Existing retractions still work
- **Validation**: No changes to retraction extraction or backend logic

## UPDATED FINDINGS (2026-05-17 17:54-17:58)

**Investigation Date**: 2026-05-17 17:54 UTC  
**Rebuild Status**: Code rebuilt with dprompt-108 (metadata-driven rel_type resolution + bidirectional lookup)

### Test Results (CRITICAL)

Even with the rebuilt code and improved `_RETRACTION_PROMPT`, retraction detection is **completely non-functional**. Clear negations are not being recognized:

**Test Case 1**: "My son Robert is 12. My daughter Emma is 11. My spouse is Victoria."
- ✅ Facts ingested
- Message: "Wait, Robert is not my son. I was mistaken."
- ❌ Retraction NOT detected (no logs)
- ❌ Robert still in family list after query
- Filter logs show: Direct `/query` call, NO `_detect_retraction_intent` logs

**Test Case 2**: "Sam is not my child."
- Clear negation with "not"
- ❌ Retraction NOT detected  
- ❌ Sam persists in family
- Filter logs show: Direct `/query` call, NO retraction attempt

**Test Case 3**: "Actually, Bob is not my son. I was mistaken."
- Negation + correction signal
- ❌ Retraction NOT detected
- ❌ Bob persists in family
- No retraction logs in Filter

### Root Cause (REVISED)

**NOT** the pattern signals (dBug-042 original hypothesis). The problem is **Layer 1 (LLM semantic detection)** is returning `is_retraction=false` for clear negations.

The `_RETRACTION_PROMPT` inclualice these exact test cases:
```
"Bob is not my son" → {"is_retraction": true, ...}
```

Yet the LLM is returning `is_retraction=false` for nearly identical user inputs.

**Evidence**:
- `_detect_retraction_intent()` called at line 1682 of faultline_function.py
- Returns `(False, {})` for all negation statements
- Pattern fallback never triggered (would log if it did)
- No LLM call logging visible (ENABLE_DEBUG likely false in production)

### Code Status

✅ **Metadata-driven rel_type resolution** (dprompt-108): 
- `_resolve_rel_type()` implemented correctly
- Queries `rel_types.inverse_rel_type` at runtime
- Syntax validated

✅ **Bidirectional granular retraction** (/retract):
- Queries `(rel_type=X AND subject_id=entity) OR (rel_type=inverse AND object_id=entity)`
- Would work IF retractions were detected

❌ **LLM retraction detection** (Layer 1):
- Not recognizing negation statements as retractions
- Possible causes:
  1. LLM not receiving prompt correctly
  2. LLM parsing response incorrectly  
  3. LLM returning wrong JSON structure
  4. Silent exception (not logged if DEBUG off)

### Critical Next Steps

1. **Enable ENABLE_DEBUG=true** in Filter valves to see actual LLM calls/responses
2. **Verify `/extract/rewrite` responses** for retraction prompts  
3. **Test LLM directly** with `_RETRACTION_PROMPT` to confirm it understands negations
4. **Check for silent failures** in _extract_retraction exception handling

## Investigation Commands

```bash
# Enable debug logging (verify current state)
ssh docker-host -x "sudo docker logs open-webui 2>&1 | grep -i 'semantic_retraction\|extract_retraction' | tail -20"

# Check if /retract endpoint was ever called (since rebuild)
ssh docker-host -x "sudo docker logs faultline 2>&1 | grep -i '/retract\|retracted\|supersede' | tail -20"

# Verify facts still have NULL superseded_at (not being retracted)
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"SELECT rel_type, COUNT(*) FROM facts 
   WHERE user_id='10d7d879-63cd-4f31-92ce-f2c9edb760ab' 
   AND superseded_at IS NULL 
   GROUP BY rel_type;\""

# Test LLM retraction prompt directly
curl -X POST "http://qwen:11434/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b",
    "messages": [
      {"role": "system", "content": "'\"'_RETRACTION_PROMPT'\"'"},
      {"role": "user", "content": "Sam is not my child."}
    ],
    "temperature": 0.0
  }' | jq '.choices[0].message.content'
```

## Related Issues

- **dBug-041**: Correction handling (is_correction flag propagation) — related but different problem
- **CLAUDE.md Inlet Short-Circuit**: alicecribes retraction flow (lines 1114-1141)
- **dprompt-108**: LLM-primary retraction detection (implemented in code, not working in practice)

---

**Priority**: CRITICAL — Retractions completely non-functional even with explicit negations.  
**Blocker**: LLM semantic detection layer (not failing gracefully, silently returning false)  
**Next Steps**: Debug LLM retraction detection with ENABLE_DEBUG=true to see actual requests/responses.
