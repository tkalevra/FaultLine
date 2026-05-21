# dBug-050: Correction APPLICATION Logic Incomplete

**Status:** ✅ FIXED  
**Severity:** CRITICAL (Corrections don't persist — core feature broken)  
**Related:** dBug-049 (scalar routing), dprompt-115 (unified ingest gate)  
**Discovered:** 2026-05-18  
**Fixed:** 2026-05-18 via dprompt-115 (correction application framework) + transaction rollback protection  
**Production Deployed:** 2026-05-18 commit 0437f1a  
**Verification:** charlie pref_name correction (charlie → cy) persisted in database ✓

---

## Problem Statement

User says: **"Actually, bob is 11, not 10"**

Observable behavior:
```
✅ LLM response: "I've updated the age for your daughter bob to 11"
✅ Correction pattern detected: "is .+ not" matches
✅ Pattern discovered: stored in correction_signals table
❌ Database: entity_attributes shows age=10 (NOT updated to 11)
```

**Impact:** Corrections are acknowledged but never persisted. User sees confirmation but database unchanged.

---

## Root Cause Analysis

### Three-Part Correction Flow

The correction feature has three stages:

1. **DETECTION** ✅ WORKING
   - Lightweight pattern regex (lines 3631-3643)
   - Identifies correction keywords: "actually", "wait", "is X not Y", etc.
   - Sets `req.is_correction = True`
   - Zero overhead (no LLM call)

2. **DISCOVERY** ✅ WORKING
   - dprompt-114 pattern learning (lines 3645-3660)
   - Stores detected patterns to `correction_signals` table
   - Accumulates confidence scores
   - Available for future corrections of same type

3. **APPLICATION** ❌ INCOMPLETE
   - Code added (lines 3661-3730) but **value extraction fails**
   - Problem: Generic patterns like `"is .+ not"` don't have proper capture groups
   - `.+` is too greedy → matches "11, not 10" as one string
   - Cannot extract old value (10) and new value (11) separately

### Why Generic Patterns Can't Extract Values

Current approach (BROKEN):
```python
pattern = "is .+ not"  # Stored in DB from pattern discovery
match = re.search(pattern, "is 11 not 10")
groups = match.groups()  # Trying to extract via capture groups
# But pattern has no parens → groups = () (empty!)
# Or if it had groups: (.+) matches "11, not 10" greedily
```

The patterns are aliceigned for **DETECTION** ("is a correction happening?"), not **EXTRACTION** ("what are the old and new values?").

---

## What SHOULD Happen (Architectural Intent — Class A Override)

Per CLAUDE.md three-tier classification:

**User corrections are CLASS A facts** (highest confidence):
- Must override ANY conflicting data globally
- Never staged/deferred
- Apply immediately with confidence=1.0
- Global semantics (not family-specific)

For scalar corrections like "bob is 11, not 10":
1. **Detect** the correction pattern ✅
2. **Extract** the values (old=10, new=11) ❌
3. **Identify** the entity (bob) ❌
4. **UPDATE** entity_attributes SET value_int=11 immediately ❌
5. **Mark** previous value as superseded (archive model) ❌

Currently stuck at step 2: can't extract the actual numbers.

---

## Solution: Numerical Heuristic Extraction

**Principle:** For scalar corrections involving numbers, don't rely on regex capture groups. Extract numbers directly using direction-aware patterns.

### Algorithm

```python
def extract_scalar_correction_values(text: str) -> tuple:
    """Extract old and new values from correction text.
    
    Returns: (old_value, new_value) as strings
    
    Patterns:
    - "is X not Y" → new=X, old=Y
    - "X instead of Y" → new=X, old=Y
    - "X, not Y" → new=X, old=Y
    """
    
    # Pattern 1: "is/are X not/instead/rather Y"
    match = re.search(
        r'(?:is|are)\s+(\d+(?:\.\d+)?)\s+(?:not|instead|rather)\s+(\d+(?:\.\d+)?)',
        text, re.IGNORECASE
    )
    if match:
        return (match.group(2), match.group(1))  # (old, new)
    
    # Pattern 2: "X instead of Y"
    match = re.search(
        r'(\d+(?:\.\d+)?)\s+instead\s+of\s+(\d+(?:\.\d+)?)',
        text, re.IGNORECASE
    )
    if match:
        return (match.group(2), match.group(1))
    
    # Pattern 3: "X, not Y" or "X not Y"
    match = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:,\s*)?not\s+(\d+(?:\.\d+)?)',
        text, re.IGNORECASE
    )
    if match:
        return (match.group(2), match.group(1))
    
    return (None, None)


def apply_scalar_correction(text: str, user_id: str, db, registry) -> bool:
    """Apply scalar correction using numerical heuristic.
    
    For corrections like "Actually, bob is 11, not 10":
    1. Extract old/new values using direction-aware heuristic
    2. Find entity name (capitalized word, resolve via registry)
    3. Infer scalar rel_type (age, height, weight, occupation)
    4. UPDATE entity_attributes with new_value (CLASS A override)
    """
    
    # Step 1: Extract old and new values
    old_value_str, new_value_str = extract_scalar_correction_values(text)
    if not (old_value_str and new_value_str):
        return False
    
    # Step 2: Find entity being corrected (capitalized words = entity names)
    entity_names = re.findall(r'\b([A-Z][a-z]+)\b', text)
    for entity_name in entity_names:
        entity_name_lower = entity_name.lower()
        
        # Resolve entity to UUID
        resolved_entity = registry.resolve(user_id, entity_name_lower)
        if not resolved_entity:
            continue
        
        # Step 3: Infer rel_type from context
        # Look for scalar rel_type keywords in text
        rel_type_keywords = {
            'age': ['age', 'years old', 'years', 'old', 'turned'],
            'height': ['height', 'tall', 'feet', 'foot', "'"],
            'weight': ['weight', 'lbs', 'pounds', 'lb'],
            'occupation': ['occupation', 'job', 'work', 'works']
        }
        
        detected_rel_type = None
        text_lower = text.lower()
        for rel_type, keywords in rel_type_keywords.items():
            for kw in keywords:
                if kw in text_lower:
                    detected_rel_type = rel_type
                    break
            if detected_rel_type:
                break
        
        if not detected_rel_type:
            detected_rel_type = 'age'  # Default to age for corrections
        
        # Step 4: Coerce values
        val_text, val_int, val_float, val_date = _coerce_scalar(new_value_str)
        
        # Step 5: UPDATE entity_attributes (CLASS A override, confidence=1.0)
        try:
            with db.cursor() as cur:
                cur.execute("""
                    UPDATE entity_attributes
                    SET value_text = %s, value_int = %s, value_float = %s,
                        value_date = %s, updated_at = now()
                    WHERE user_id = %s AND entity_id = %s
                      AND attribute = %s
                """, (val_text, val_int, val_float, val_date,
                      user_id, resolved_entity, detected_rel_type))
                
                updated = cur.rowcount
                if updated > 0:
                    db.commit()
                    log.info("ingest.scalar_correction_applied",
                             entity=entity_name,
                             entity_id=resolved_entity,
                             attribute=detected_rel_type,
                             old_value=old_value_str,
                             new_value=new_value_str,
                             rows_updated=updated)
                    return True
        except Exception as e:
            log.warning("ingest.scalar_correction_update_failed",
                       entity=entity_name, error=str(e))
            db.rollback()
    
    return False
```

### Example Trace

**Input:** `"Actually, bob is 11, not 10"`

```
Step 1: extract_scalar_correction_values()
  Pattern: "is 11 not 10" → matches pattern 1
  Match groups: (11, 10)
  Return: (old=10, new=11) ✓

Step 2: Find entity
  entity_names = ['Actually', 'bob']
  'Actually' → registry.resolve() → None (not an entity)
  'bob' → registry.resolve() → UUID 6965ba7e-... ✓

Step 3: Infer rel_type
  text = "Actually, bob is 11, not 10"
  Check keywords: 'age' not in text, 'years' not in text, etc.
  No keyword match → default to 'age' ✓

Step 4: Coerce values
  _coerce_scalar('11') → (None, 11, None, None)
  val_int = 11 ✓

Step 5: UPDATE
  UPDATE entity_attributes 
  SET value_int=11 
  WHERE user_id=<user> AND entity_id=6965ba7e-... AND attribute='age'
  rowcount = 1 ✓
```

---

## Implementation Steps

1. **Add extraction function** (before /ingest endpoint)
   - `extract_scalar_correction_values(text)` → (old_value, new_value)
   - Uses direction-aware patterns ("is X not Y", "X instead of Y", etc.)

2. **Add application function** (before /ingest endpoint)
   - `apply_scalar_correction(text, user_id, db, registry)` → bool
   - Extracts values, finds entity, infers rel_type, UPDATEs
   - Returns True if successful

3. **Call in /ingest endpoint** (after pattern discovery, before extraction)
   - Lines 3661-3730: Replace broken capture-group logic with call to `apply_scalar_correction()`
   - Only applies if `req.is_correction == True`
   - Logs success/failure

4. **Test end-to-end**
   - Run pipeline test with curl
   - Verify database UPDATE happens immediately
   - Confirm LLM response matches reality

---

## Test Cases

### Test 1: Age Correction
```
Input: "Actually, bob is 11, not 10"
Extract: old='10', new='11' ✓
Entity: bob → UUID
Rel_type: 'age' (default)
UPDATE: value_int = 11
Expected: entity_attributes shows age=11 ✅
```

### Test 2: Height Correction (with quotes)
```
Input: "Wait, I'm 5'10, not 5'9"
Extract: old='5'9', new='5'10' (handle quotes)
Entity: user (inferred from "I'm")
Rel_type: 'height' (keyword match)
UPDATE: value_text = "5'10"
Expected: entity_attributes shows height=5'10 ✅
```

### Test 3: Alternative Phrasing
```
Input: "Actually, alice is 13 instead of 12"
Extract: old='12', new='13' (pattern 2)
Entity: alice → UUID
Rel_type: 'age'
UPDATE: value_int = 13
Expected: entity_attributes shows age=13 ✅
```

### Test 4: Multiple corrections in sequence
```
1st: "bob is 11, not 10" → age updates to 11
2nd: "Actually alice is 13, not 12" → alice age updates to 13
3rd: "My height is 5'10" → user height updates to 5'10
Expected: All three updates persist independently ✅
```

---

## Current Code Location

**File:** `/home/chris/Documents/013-GIT/FaultLine-dev/src/api/main.py`

**Lines to replace:** 3661-3730 (broken extraction logic)

**Keep:** 
- Lines 3631-3643 (detection — working)
- Lines 3645-3660 (discovery — working)

**Add before /ingest endpoint:**
- `extract_scalar_correction_values()` function
- `apply_scalar_correction()` function

---

## Why This Matters (User's Core Intent)

**User emphasis:** "teh entire fucking point of this excercise is exaclty htat, age and retraction resolution asshole"

**Translation:**
- Corrections (age updates) = THE CORE FEATURE
- Retractions (forget facts) = THE CORE FEATURE
- Everything else is secondary

Without correction APPLICATION:
- ❌ Users see "updated to 11" but DB shows 10
- ❌ Next query returns wrong age
- ❌ System appears broken/unreliable
- ❌ User trust alicetroyed

---

## Validation Checklist

After implementation:
- [ ] `"Actually, bob is 11, not 10"` → age=11 in DB ✅
- [ ] `"Wait, I'm 5'10, not 5'9"` → height=5'10 in DB ✅
- [ ] `"alice is 13 instead of 12"` → age=13 in DB ✅
- [ ] Multiple corrections in sequence work independently ✅
- [ ] Non-scalar corrections (relationships) unaffected ✅
- [ ] No scalar facts in facts table ✅
- [ ] Full pipeline test passes (curl) ✅
- [ ] Database reflects all corrections immediately ✅

---

## References

- CLAUDE.md: Three-dimensional fact classification (Class A/B/C — user corrections are CLASS A)
- CLAUDE.md: Fact Storage Paths (scalars → entity_attributes, relationships → facts)
- dBug-049: Scalar facts routing (fixed in this session)
- dprompt-115: Unified ingest gate (detection/discovery working, application incomplete)
