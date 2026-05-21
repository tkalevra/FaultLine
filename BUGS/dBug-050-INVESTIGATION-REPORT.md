# dBug-050 Investigation Report: Correction APPLICATION Complete Failure Analysis

**Depth:** Deep code investigation + architectural alignment  
**Scope:** Correction flow, entity_attributes schema, Class A semantics, user as source of truth  
**Focus:** Why corrections fail + how to fix respecting USER-STATED FACTS as authoritative

---

## Executive Summary

**Root Finding:** Correction APPLICATION is 100% non-functional due to capture group extraction aliceign flaw. Lines 3689-3734 cannot extract values, so UPDATE never executes. Additionally, scalar corrections lack proper Class A semantics (user override, audit trail, immediate application).

**Impact:** User says "bob is 11 not 10" → LLM confirms → DB shows 10 (unchanged). System appears broken/unreliable.

**Why This Matters:** User emphasized corrections are THE CORE FEATURE. Without working corrections, system has zero credibility.

---

## Finding 1: Capture Group Extraction Failure (CRITICAL)

### Code Location
Lines 3689-3690, 3703-3704:
```python
groups = match.groups()                      # Line 3689 — ALWAYS EMPTY
if len(groups) >= 2:                         # Line 3690 — ALWAYS FALSE
    # ... extraction code never runs
```

### Why It Fails

**Pattern storage:** `correction_signals` table stores patterns like:
```
pattern = "is .+ not"
pattern = "actually"
pattern = "wait"
```

**No capture groups:** These patterns have ZERO parentheses:
```
"is .+ not"        # No groups → match.groups() = ()
"actually"         # No groups → match.groups() = ()
"wait"             # No groups → match.groups() = ()
```

**Code expectation:** Lines 3688-3692 expect ≥2 capture groups:
```python
# Extract capture groups (pattern should have old/new values as groups)  ← COMMENT IS WRONG
groups = match.groups()                      # Returns () for "is .+ not"
if len(groups) >= 2:                         # len(()) = 0 → FAILS
    old_val_str = groups[0]
    new_val_str = groups[1]
```

**Result:** Condition `if len(groups) >= 2:` is **NEVER TRUE**. The entire correction UPDATE block (lines 3703-3733) is skipped.

### Test Case Proving Failure

Input: `"Actually, bob is 11, not 10"`

Trace:
```
Line 3683: match = re.search("is .+ not", text) → matches "is 11, not 10" ✅
Line 3689: groups = match.groups() → () (empty tuple) ✅
Line 3690: if len(()) >= 2 → FALSE ❌
Line 3691-3732: [SKIPPED — never executes]
Database: bob.age still = 10 ❌
```

---

## Finding 2: aliceign Mismatch — Detection vs Extraction

### The Pattern Paradox

**Patterns aliceigned for DETECTION (correct use):**
```python
# "is there a correction happening?" — YES/NO
pattern = "is .+ not"       # Detects: "is 11, not 10"
pattern = "actually"        # Detects: "Actually, ..."
```

**Patterns misused for EXTRACTION (broken):**
```python
# "extract the old and new values" — CAN'T DO THIS
pattern = "is .+ not"
match = re.search(pattern, "is 11, not 10")
# Can't extract 11 and 10 separately without proper capture groups
```

### Why Generic Patterns Can't Extract

Even IF patterns had capture groups like `(is .+ not)`:
```python
match = re.search("(is .+ not)", "is 11, not 10")
groups[0] = "is 11, not 10"  # Still one group, not two values!
```

The pattern would need:
```python
# To extract old and new separately:
r"is\s+(\d+)\s+not\s+(\d+)"  # Specific, not generic
# Result: groups[0]="11", groups[1]="10"
```

But that's TOO SPECIFIC for a generic correction pattern. Generic patterns (`is .+ not`) are for detection, specific patterns (with capture groups) are for extraction. You can't use one for both.

---

## Finding 3: Category → Rel_type Inference is Broken

### Code Location
Lines 3703-3704:
```python
rel_type_from_category = pattern_category or "age"
```

### The Problem

**Database state:** Correction_signals patterns have category field:
```sql
SELECT pattern, pattern_type, category FROM correction_signals:
| pattern        | pattern_type | category |
| is .+ not      | negation     | family   |
| actually       | reclarification | NULL  |
| wait           | reclarification | NULL  |
```

**Code assumption:** pattern_category = rel_type (WRONG)
```python
rel_type_from_category = pattern_category or "age"
# If pattern.category = "family" → UPDATE WHERE attribute = "family" (WRONG!)
# If pattern.category = NULL → UPDATE WHERE attribute = "age" (SOMETIMES RIGHT)
```

### Issue 1: Category ≠ Rel_type

- `category` = "family" (domain/grouping) 
- `rel_type` = "age", "height", "weight", "occupation" (scalar attribute)
- These are NOT the same!

### Issue 2: Defaulting Everything to "age"

```python
rel_type_from_category = pattern_category or "age"
```

This means:
```
Correction: "Actually, I'm 5'10 not 5'9" (HEIGHT)
Pattern: "is .+ not" → category="family"
rel_type_from_category = "family" or "age" = "family" ❌
UPDATE WHERE attribute = "family" → NO MATCH ❌

Correction: "My job is Engineer not Manager" (OCCUPATION)
Pattern: "is .+ not" → category="family"
rel_type_from_category = "family" ❌
UPDATE WHERE attribute = "occupation" → NO MATCH ❌
```

**Result:** Only age corrections might work (by accident, with fallback). All others fail.

---

## Finding 4: Entity Identification is Fragile

### Code Location
Lines 3696-3702:
```python
entity_matches = re.findall(r'\b([A-Z][a-z]+)\b', req.text)
for entity_name in entity_matches:
    entity_name_lower = entity_name.lower()
    resolved_entity = registry.resolve(req.user_id, entity_name_lower)
    if resolved_entity:
        # ... proceed with update
```

### Issue 1: "Actually" Matches as Entity Name

Input: `"Actually, bob is 11, not 10"`

```python
entity_matches = re.findall(r'\b([A-Z][a-z]+)\b', text)
# Result: ["Actually", "bob"]
```

Loop iteration 1:
```python
entity_name_lower = "actually"
resolved_entity = registry.resolve(user_id, "actually")
# Returns None (not a real entity)
# Continue to next
```

Loop iteration 2:
```python
entity_name_lower = "bob"
resolved_entity = registry.resolve(user_id, "bob") → UUID ✅
# Proceed with update
```

**Waste:** First iteration is wasted on non-entity.

### Issue 2: Multiple Real Entities → First One Wins

Input: `"Actually, alice is 12 and bob is 11, not 10"`

```python
entity_matches = ["Actually", "alice", "bob"]
# Updates alice first (12 → 11?) ❌ WRONG ENTITY
# Then breaks (line 3734)
```

**Result:** Ambiguous input → wrong entity gets updated.

### Issue 3: No Validation That Entity Matches Context

```python
# What if user says: "John is 11 not 10"?
# CODE WILL:
# 1. Find "John" as entity
# 2. Resolve to user UUID
# 3. Update age to 11 (probably correct)
# 4. But what if meant: "My dad John is 11"? (different entity)
```

No way to validate which John, which context.

---

## Finding 5: Class A Semantics NOT IMPLEMENTED

### What CLASS A Should Do (Per CLAUDE.md)

**User-stated facts are highest confidence:**
- Confidence = 1.0 (authoritative, not inherited from pattern)
- Override ALL conflicting data globally
- Apply immediately (not staged)
- Mark old value as superseded/archived
- Audit trail: track who said what, when

### What Current Code Does

**Lines 3711-3723:**
```python
UPDATE entity_attributes
SET value_text = %s, value_int = %s,
    value_float = %s, value_date = %s,
    updated_at = now()
WHERE user_id = %s AND entity_id = %s
  AND attribute = %s
```

**Missing:**
- ❌ Confidence tracking (should be 1.0 for user correction)
- ❌ Audit trail (no provenance = "user_correction" or similar)
- ❌ Supersession tracking (old value not marked as superseded)
- ❌ valid_until on old value (for archive model)
- ❌ valid_from on new value (for temporality)
- ❌ Retraction support (should DELETE entity_attributes rows on retraction, like it does facts)

### Schema Supports It But Code Doesn't Use It

```sql
entity_attributes schema:
- provenance TEXT           ← Unused (should be "user_correction")
- valid_from TIMESTAMP      ← Unused (old value valid_until, new value valid_from)
- valid_until TIMESTAMP     ← Unused
```

---

## Finding 6: Retraction Path Broken for Scalars

### /retract Endpoint Only Handles facts Table

Lines 6645-6676 (granular retraction):
```python
query = "UPDATE facts SET superseded_at = now() WHERE ..."
cur.execute(query, params)  # Only updates facts table
```

### Missing: entity_attributes Handling

If user says: `"Forget about bob's age"`

- ✅ Will retract age facts from `facts` table (if any were there)
- ❌ WON'T touch `entity_attributes` (where the real age is stored)
- ❌ Age still shows in queries (via `_attributes_to_facts()`)

**Result:** Retraction fails for scalar facts.

---

## Finding 7: No Relationship Between Detection & Application

### Detection Works (Lines 3631-3643)

```python
if is_likely_correction:
    req.is_correction = True  # ✅ Correctly set
```

### Application Ignored (Lines 3665-3750)

```python
if req.is_correction and req.text:
    # ... tries to apply, but fails silently at line 3690
    # ... never logs that application failed
    # ... proceeds to normal extraction
```

### Problem: Correction Falsely Claimed as Applied

LLM response inclualice: `"I've updated the age for your daughter bob to 11"`

But in code:
- ❌ Correction APPLICATION never succeeds
- ❌ UPDATE never runs
- ❌ Database unchanged
- ❌ Yet LLM promises update happened

**This is USER TRUST DESTRUCTION.**

---

## Recommendation 1: Replace Extraction Logic (IMMEDIATE FIX)

### Current (Broken)
```python
# Lines 3661-3750: Generic pattern matching + capture group extraction
if req.is_correction and req.text:
    for pattern_str, pattern_category, pattern_conf in patterns:
        match = re.search(pattern_str, req.text, re.IGNORECASE)
        if match:
            groups = match.groups()  # ← ALWAYS EMPTY
            if len(groups) >= 2:     # ← ALWAYS FALSE
                # ... extraction never runs
```

### Replacement: Numerical Heuristic
```python
# Lines 3661-3750: Direction-aware numerical extraction
if req.is_correction and req.text:
    old_val, new_val = extract_scalar_correction_values(req.text)
    if old_val and new_val:
        entity_name = extract_entity_from_context(req.text, req.user_id)
        rel_type = infer_rel_type_from_context(req.text)
        if entity_name and rel_type:
            success = apply_scalar_correction(
                user_id=req.user_id,
                entity_name=entity_name,
                rel_type=rel_type,
                new_value=new_val,
                db=db,
                registry=registry
            )
            if success:
                log.info("ingest.correction_applied_class_a", ...)
```

---

## Recommendation 2: Implement CLASS A Semantics

### Update entity_attributes WITH Audit Trail

```python
def apply_scalar_correction(user_id, entity_id, attribute, new_value, db):
    """Apply CLASS A correction with full audit trail."""
    val_text, val_int, val_float, val_date = _coerce_scalar(new_value)
    
    with db.cursor() as cur:
        # Mark old value as superseded (if exists)
        cur.execute("""
            UPDATE entity_attributes
            SET valid_until = now()
            WHERE user_id = %s AND entity_id = %s 
              AND attribute = %s AND valid_until IS NULL
        """, (user_id, entity_id, attribute))
        
        # Insert new value OR update existing (if not interval-based)
        cur.execute("""
            INSERT INTO entity_attributes
            (user_id, entity_id, attribute, value_text, value_int, 
             value_float, value_date, provenance, valid_from, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), now())
            ON CONFLICT (user_id, entity_id, attribute) 
            DO UPDATE SET
                value_text = EXCLUDED.value_text,
                value_int = EXCLUDED.value_int,
                value_float = EXCLUDED.value_float,
                value_date = EXCLUDED.value_date,
                provenance = 'user_correction',
                updated_at = now(),
                valid_from = now(),
                valid_until = NULL
        """, (user_id, entity_id, attribute, val_text, val_int, 
              val_float, val_date, 'user_correction'))
        
        db.commit()
        return cur.rowcount > 0
```

---

## Recommendation 3: Fix Rel_type Inference

### Replace Category Inference
```python
# WRONG: rel_type_from_category = pattern_category or "age"

# RIGHT: Infer from text context
def infer_scalar_rel_type(text):
    """Infer which scalar rel_type is being corrected."""
    text_lower = text.lower()
    
    # Check for specific keywords
    if any(k in text_lower for k in ['age', 'years old', 'years', 'turned']):
        return 'age'
    if any(k in text_lower for k in ['height', 'tall', 'feet', 'foot', "'"]):
        return 'height'
    if any(k in text_lower for k in ['weight', 'lbs', 'pounds', 'lb']):
        return 'weight'
    if any(k in text_lower for k in ['occupation', 'job', 'work', 'works']):
        return 'occupation'
    
    return 'age'  # Safe default
```

---

## Recommendation 4: Fix Entity Extraction

### Multi-Entity Ambiguity Resolution
```python
def extract_entity_from_correction(text, user_id, registry, req_text_full):
    """Extract THE entity being corrected (not all capitalized words)."""
    
    # Strategy 1: Look for explicit possession ("my X", "my son X")
    match = re.search(r'\b(?:my|your|his|her)\s+(?:son|daughter|wife|husband|pet)\s+([A-Z][a-z]+)\b', 
                     text, re.IGNORECASE)
    if match:
        return registry.resolve(user_id, match.group(1).lower())
    
    # Strategy 2: Look for "X is/are"
    match = re.search(r'\b([A-Z][a-z]+)\s+(?:is|are)\s+\d+', text)
    if match:
        return registry.resolve(user_id, match.group(1).lower())
    
    # Strategy 3: Default to user (for "I am X")
    if 'i ' in text.lower() or i'm' in text.lower():
        return user_id
    
    # Fallback: Not enough context to determine
    return None
```

---

## Recommendation 5: Add Retraction Support for entity_attributes

### /retract Should Handle Scalars

```python
# In /retract endpoint, granular scope handling:
if scope_level == "granular":
    rel_type = req.scope.get("rel_type")  # e.g., "age"
    
    # Check if rel_type is scalar
    if rel_type and _is_scalar_rel_type(rel_type):
        # Delete from entity_attributes (scalar retraction = deletion)
        cur.execute("""
            DELETE FROM entity_attributes
            WHERE user_id = %s AND attribute = %s 
              AND entity_id = %s
        """, (req.user_id, rel_type, subject_uuid))
    else:
        # Delete from facts (relationship retraction = supersession)
        cur.execute("""
            UPDATE facts SET superseded_at = now() 
            WHERE user_id = %s AND rel_type = %s
        """, (...))
```

---

## Recommendation 6: Validation & Logging

### Add Explicit Failure Logging

```python
# Lines 3661-3750: Detailed logging of why extraction fails
if req.is_correction and req.text:
    old_val, new_val = extract_scalar_correction_values(req.text)
    
    if not old_val or not new_val:
        log.warning("ingest.correction_extraction_failed",
                   reason="no_old_new_values_found",
                   text=req.text[:100])
        # Continue to normal extraction (graceful fallback)
    
    entity_name = extract_entity_from_correction(req.text, req.user_id, registry, req.text)
    
    if not entity_name:
        log.warning("ingest.correction_entity_extraction_failed",
                   reason="ambiguous_or_no_entity",
                   text=req.text[:100])
        # Continue to normal extraction
    
    rel_type = infer_scalar_rel_type(req.text)
    
    if not rel_type:
        log.warning("ingest.correction_rel_type_inference_failed",
                   reason="unknown_rel_type",
                   text=req.text[:100])
        # Continue to normal extraction
    
    # Only proceed if ALL three extracted successfully
    if old_val and new_val and entity_name and rel_type:
        success = apply_scalar_correction(...)
        if not success:
            log.error("ingest.correction_database_update_failed",
                     entity=entity_name, rel_type=rel_type, new_value=new_val)
```

---

## Recommendation 7: USER AS SOURCE OF TRUTH — Global Architecture

### Principle: Correction = CLASS A Override

When user corrects a fact:
1. **Immediate effect:** Database updated instantly (not staged)
2. **Confidence = 1.0:** User is authoritative, not pattern-inferred
3. **Global scope:** Overrialice ALL conflicting data (not family-specific)
4. **Audit trail:** Marked as `provenance='user_correction'`
5. **Supersession:** Old value marked as obsolete (valid_until)
6. **Query respect:** /query must return corrected value (via valid_until filtering)

### Current Gap

Schema supports it (`provenance`, `valid_from`, `valid_until`) but code doesn't use it.

**Fix:** Ensure every correction UPDATE inclualice:
```python
UPDATE entity_attributes
SET ...values...,
    provenance = 'user_correction',
    valid_until = NULL,
    valid_from = now(),
    updated_at = now()
```

---

## Testing Plan

### Test 1: Basic Age Correction
```
Input: "Actually, bob is 11, not 10"
Expected:
  ✅ Old value extraction: old='10'
  ✅ New value extraction: new='11'
  ✅ Entity resolution: bob → UUID
  ✅ Rel_type inference: age
  ✅ Database UPDATE: value_int=11
  ✅ Query returns: age=11
```

### Test 2: Height Correction
```
Input: "Wait, I'm 5'10, not 5'9"
Expected:
  ✅ Extract: old='5'9', new='5'10'
  ✅ Entity: user (from "I'm")
  ✅ Rel_type: height (keyword match)
  ✅ Database: value_text='5\'10'
  ✅ Next query shows: height=5'10
```

### Test 3: Ambiguous Input
```
Input: "alice and bob are 13 not 12"
Expected:
  ❌ Too ambiguous → skip correction
  ✅ Log warning: "ambiguous_entity"
  ✅ Proceed to normal extraction
```

### Test 4: Retraction of Scalar
```
Input: "Forget bob's age"
Expected:
  ✅ /retract endpoint deletes entity_attributes row
  ✅ Next query: age field missing/empty
```

---

## Impact Summary

### What's Broken (Current State)
- ❌ 100% of correction applications fail silently
- ❌ Database never updates
- ❌ User trust alicetroyed
- ❌ System appears unreliable

### What Will Work (After Fix)
- ✅ Corrections applied immediately (CLASS A semantics)
- ✅ Audit trail preserved (provenance, valid_from/until)
- ✅ Retraction works for scalars
- ✅ Multiple corrections independent
- ✅ System reliable, trustworthy

---

## Implementation Priority

1. **IMMEDIATE (Critical Path):**
   - Implement `extract_scalar_correction_values()` 
   - Implement `apply_scalar_correction()` with Class A semantics
   - Replace lines 3661-3750 with new extraction logic
   - Test: Basic age/height corrections work end-to-end

2. **SOON (Session 2):**
   - Fix entity extraction (multi-entity ambiguity)
   - Fix rel_type inference (text keywords, not category)
   - Add retraction support for entity_attributes
   - Full regression test suite

3. **FOLLOW-UP:**
   - Temporal filtering in /query (/valid_from, valid_until)
   - Archive model UI/query filtering
   - Historical "what was my age on date X?" queries

---

## References

- CLAUDE.md: Three-dimensional fact classification (Class A = user-stated)
- CLAUDE.md: Archive model (dprompt-91)
- dBug-049: Scalar routing (fixed in this session)
- dBug-050: Full bug report with solution pseudocode
