# dBug-050 Investigation Report (CORRECTED): Correction APPLICATION aliceign Flaw

**Corrected Understanding:** Patterns are TRIGGERS/GATES (smart), not value extractors. LLM extracts values (dumb extraction). The system learns patterns via dprompt-114. Current code violates this architecture.

**Status:** 🔴 CRITICAL — Code tries to extract from patterns (wrong), should use patterns as confidence gates + LLM extraction (right)

---

## Corrected Architectural Understanding

### Smart Ingest, Dumb Extraction

**DUMB EXTRACTION (Simple, pattern-based):**
```
Pattern: "is .+ not"
Purpose: TRIGGER/FLAG — "Is there a correction here?"
NOT: Extract values from pattern
Instead: Gate/confidence signal
```

**SMART INGEST (LLM-based):**
```
When pattern triggers + passes confidence gate:
→ Call LLM: "Extract the correction from this text"
→ LLM returns: {"entity": "bob", "attribute": "age", "old": "10", "new": "11"}
→ Apply UPDATE using LLM output
→ dprompt-114 learns this pattern for future use
```

### The Elegant aliceign (What Should Happen)

```python
# Phase 1: Pattern-based trigger (DUMB — simple regex)
if re.search(r"is\s+.+\s+not", text):
    pattern_matched = "is .+ not"
    confidence = get_pattern_confidence(pattern_matched)  # 0.8
    log.info("correction_pattern_triggered", pattern=pattern_matched, confidence=confidence)
else:
    return  # Not a correction, proceed normally

# Phase 2: Confidence gate (DUMB — threshold check)
if confidence < MIN_CORRECTION_CONFIDENCE:
    return  # Don't trust this pattern, skip
log.info("correction_confidence_passed", confidence=confidence)

# Phase 3: Smart extraction (SMART — LLM)
extraction = await llm_extract_correction_details(text, user_id)
# LLM returns:
# {
#   "entity_name": "bob",
#   "entity_id": "6965ba7e-...",
#   "attribute": "age",
#   "old_value": "10",
#   "new_value": "11",
#   "confidence": 0.95
# }

# Phase 4: Apply CLASS A correction (SMART — database)
apply_scalar_correction(
    user_id=user_id,
    entity_id=extraction["entity_id"],
    attribute=extraction["attribute"],
    new_value=extraction["new_value"],
    confidence=extraction["confidence"],  # Trust LLM, not pattern
    provenance="user_correction"
)
log.info("correction_applied_class_a", entity=extraction["entity_name"], 
         attribute=extraction["attribute"], new_value=extraction["new_value"])

# Phase 5: Pattern learning (SMART — grow correction_signals)
store_correction_pattern(
    pattern=pattern_matched,
    confirmed=True,  # User said it, LLM extracted it → confirm this pattern
    confidence=confidence,
    extracted_rel_types=[extraction["attribute"]]  # Learn: "is .+ not" matches age
)
```

---

## FINDING 1: Code Violates Smart/Dumb Architecture (CRITICAL FLAW)

### Current Code (BROKEN)

**Lines 3683-3734:**
```python
# ❌ WRONG: Treats pattern as VALUE EXTRACTOR
match = re.search(pattern_str, req.text, re.IGNORECASE)  # Pattern matches
if match:
    groups = match.groups()  # ❌ Tries to extract values from pattern
    if len(groups) >= 2:     # ❌ Expects: groups[0]=old_val, groups[1]=new_val
        old_val_str = groups[0]
        new_val_str = groups[1]  # NEVER HAPPENS (patterns have no capture groups)
        # ... UPDATE never runs
```

**The Problem:**
- Pattern `"is .+ not"` is a TRIGGER, not a value matcher
- Code assumes pattern teaches HOW to extract: `(old_value) .+ (new_value)` in parentheses
- Pattern has no parentheses → `groups = ()` (empty)
- Code fails silently → UPDATE never happens

### What Should Happen Instead

```python
# ✅ RIGHT: Pattern is just a GATE/TRIGGER
match = re.search(pattern_str, req.text, re.IGNORECASE)  # Pattern matches
if match:
    # Pattern triggered! Confidence says: should we proceed?
    pattern_confidence = get_pattern_confidence(pattern_str)
    
    if pattern_confidence >= threshold:  # Confidence GATE
        # YES, proceed to smart extraction (LLM)
        extraction = await llm_extract_correction(req.text, req.user_id)
        # LLM does the heavy lifting: finds entity, attribute, old/new values
        
        if extraction:
            apply_scalar_correction(...)  # Apply
            confirm_correction_pattern(pattern_str)  # Learn: this pattern works!
```

**Key insight:** Patterns don't extract values, they SIGNAL "there's probably a correction here". LLM extracts actual values.

---

## FINDING 2: Confidence Gate Misunderstood

### Current Code (WRONG)

**Lines 3669-3676:**
```python
cur.execute("""
    SELECT pattern, category, confidence
    FROM correction_signals
    WHERE user_id = %s
    ORDER BY confidence DESC, created_at DESC
    LIMIT 20
""", (req.user_id,))
patterns = _cor_cur.fetchall()

# Then tries to use 'confidence' to infer rel_type (WRONG USE)
for pattern_str, pattern_category, pattern_conf in patterns:
    # ...
    rel_type_from_category = pattern_category or "age"  # ❌ Wrong use of pattern data
```

**What confidence should do:**
```python
# ✅ RIGHT: Confidence is a GATE
pattern_confidence = pattern_conf  # e.g., 0.8
if pattern_confidence >= MIN_CORRECTION_CONFIDENCE:  # GATE CHECK
    # This pattern is trusted enough, proceed to extraction
    proceed_to_llm_extraction = True
else:
    # This pattern is low-confidence, skip
    proceed_to_llm_extraction = False
```

**Current misuse:**
- ❌ Tries to infer rel_type from `pattern_category` (wrong)
- ❌ Uses confidence as data (wrong)
- ✅ Should use confidence as a GATE threshold

---

## FINDING 3: LLM Should Do Extraction (Missing Component)

### Current Architecture (Incomplete)

```
User: "Actually, bob is 11, not 10"
       ↓
Detection: Pattern "is .+ not" matches ✅
       ↓
Extraction: Try regex capture groups ❌ (FAILS)
       ↓
Database: UPDATE never happens ❌
```

### Correct Architecture (What's Missing)

```
User: "Actually, bob is 11, not 10"
       ↓
Detection: Pattern "is .+ not" matches ✅
       ↓
Confidence Gate: confidence=0.8 > threshold=0.4 ✅
       ↓
Smart Extraction: Call LLM → "entity=bob, attr=age, old=10, new=11" ✅
       ↓
Validate: LLM extraction confidence=0.95 ✅
       ↓
Apply: UPDATE entity_attributes (CLASS A) ✅
       ↓
Learn: Store pattern + confirm it works ✅
```

### What's Missing: LLM Extraction Call

**Lines 3661-3750 should include:**
```python
# After pattern matches + confidence gate passes:
if pattern_confidence >= threshold:
    # Call LLM to extract correction details
    llm_url = _get_llm_url()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{llm_url}/v1/chat/completions",
            json={
                "model": llm_model,
                "messages": [{
                    "role": "user",
                    "content": f"""Extract correction details from this text:
                    "{req.text}"
                    
                    Return JSON with:
                    - entity_name: who is being corrected (e.g., "bob")
                    - attribute: what is being corrected (e.g., "age")
                    - old_value: previous value (e.g., "10")
                    - new_value: corrected value (e.g., "11")
                    - confidence: how sure are you (0.0-1.0)
                    """
                }],
                "temperature": 0.1
            }
        )
    
    extraction = parse_json(response.json()["choices"][0]["message"]["content"])
    
    if extraction and extraction["confidence"] > 0.7:
        # LLM is confident → apply correction
        apply_scalar_correction(
            user_id=req.user_id,
            entity_name=extraction["entity_name"],
            attribute=extraction["attribute"],
            new_value=extraction["new_value"],
            confidence=extraction["confidence"],  # Trust LLM, not pattern
            provenance="user_correction"
        )
```

---

## FINDING 4: dprompt-114 Pattern Learning Not Leveraged

### Current State

**Pattern discovery works (lines 3645-3660):**
```python
# Stores patterns to correction_signals table ✅
cur.execute("""
    INSERT INTO correction_signals
    (pattern, pattern_type, priority, confidence, example_usage, created_at)
    VALUES (%s, %s, %s, %s, %s, now())
""", (pattern, pattern_type, priority, confidence, example_usage))
```

**But patterns never affect downstream logic (MISSED OPPORTUNITY):**
```
correction_signals table grows ✅
Patterns stored with confidence ✅
But: Code doesn't USE these patterns intelligently ❌
  - Doesn't gate on confidence ❌
  - Doesn't learn which patterns correlate with which rel_types ❌
  - Doesn't validate corrections to improve pattern confidence ❌
```

### Missing: Pattern → Rel_type Mapping

**The elegant aliceign:**
```
Pattern: "is .+ not"
Discovered in: "bob is 11, not 10"  
Extracted rel_type: age
Corrected: ✅ (user confirmed)

Next time pattern "is .+ not" triggers:
→ Suggest rel_type = age (learned from this correction)
→ Increase confidence: 0.8 → 0.85
```

**Missing schema column:**
```sql
-- In correction_signals:
applicable_rel_types TEXT[]  -- ["age"] (learned from corrections)
-- But never populated!
```

### How It Should Work

```python
# After LLM extracts and correction is confirmed:
update_pattern_metadata(
    pattern="is .+ not",
    extracted_rel_type=extraction["attribute"],  # "age"
    success=True,  # User confirmed this worked
    # Increase confidence: this pattern works for age corrections
)

# Result: Next time "is .+ not" matches:
# → Check applicable_rel_types = ["age"]
# → Set suggested rel_type = "age"
# → Increase confidence for pattern
```

**Current gap:** Pattern learning stores patterns but never learns WHAT they correct.

---

## FINDING 5: Class A Semantics Lost in Extraction

### The Issue

**Current code (lines 3706-3720):**
```python
# ❌ Uses pattern confidence, not user/LLM confidence
val_text, val_int, val_float, val_date = _coerce_scalar(new_val_str)
UPDATE entity_attributes
SET value_text = %s, value_int = %s, ...
WHERE user_id = %s AND entity_id = %s AND attribute = %s
# Missing: provenance = 'user_correction', confidence tracking
```

**What CLASS A should be:**
```python
# ✅ User-stated fact = highest confidence (1.0)
# ✅ But when extracted via LLM, use LLM confidence
UPDATE entity_attributes
SET value_int = %s,
    provenance = 'user_correction',
    confidence = extraction["confidence"],  # Trust LLM (0.95), not pattern (0.8)
    updated_at = now()
WHERE user_id = %s AND entity_id = %s AND attribute = %s
```

**The principle:**
- User said it (Class A) → authoritative
- But we extract via LLM (not perfect) → use LLM confidence
- Pattern just triggered, didn't extract → don't use pattern confidence for value

---

## FINDING 6: Pattern Growth Not Validated

### Current State

**dprompt-114 stores patterns (lines 3645-3660):**
```python
# Every correction attempt stores patterns
cur.execute("""
    INSERT INTO correction_signals
    (pattern, pattern_type, priority, confidence, example_usage, created_at)
    VALUES ...
    ON CONFLICT (pattern) DO NOTHING  # ← Pattern dedup works
""")
```

**But no validation loop:**
```
❌ Pattern stored with initial confidence (0.8)
❌ No tracking of: was this pattern correct?
❌ No confidence updates based on validation
❌ No learning that "is .+ not" specifically matches ages
❌ No feedback loop: successful corrections → increase pattern confidence
```

### Missing: Correction Validation Loop

```python
# After correction applied:
cur.execute("""
    UPDATE correction_signals
    SET confidence = confidence + 0.05,  -- Increase confidence
        success_count = success_count + 1
    WHERE pattern = %s
""", (pattern_matched,))

# After 5 successful corrections with pattern:
# Pattern confidence: 0.8 → 0.85 → 0.90 → 0.95 → 1.0
# System learns: this pattern is very reliable
```

---

## FINDING 7: Silent Failure vs Graceful Degradation

### Current Code (Lines 3689-3700)

```python
groups = match.groups()          # ← FAILS SILENTLY
if len(groups) >= 2:             # ← Always false
    # Extraction code never runs
    # No log message, no warning, no error
    # Just silently skips to next pattern
    
break  # ← Breaks after FIRST pattern match, even if extraction failed
# Never tries other patterns
# Never falls back to normal extraction
```

**Result:** Zero visibility into failure.

### What Should Happen

```python
# Pattern matched → confidence gate
if pattern_confidence >= threshold:
    log.info("correction_pattern_triggered",
             pattern=pattern_str,
             confidence=pattern_confidence)
    
    # Call LLM for extraction
    extraction = await llm_extract_correction(req.text, req.user_id)
    
    if extraction is None:
        log.warning("correction_extraction_failed",
                   pattern=pattern_str,
                   reason="llm_returned_none")
        continue  # Try next pattern
    
    if extraction["confidence"] < 0.7:
        log.warning("correction_extraction_low_confidence",
                   pattern=pattern_str,
                   confidence=extraction["confidence"])
        continue  # Try next pattern
    
    # Apply correction
    success = apply_scalar_correction(...)
    if success:
        log.info("correction_applied_successfully", ...)
        confirm_pattern(pattern_str)  # Learn this pattern works!
        break  # Success → stop trying patterns
    else:
        log.error("correction_database_failed", ...)
        continue  # Try next pattern
```

**Key difference:** Explicit logging at each step, graceful fallback, pattern validation.

---

## RECOMMENDATION 1: Implement Correct Architecture (IMMEDIATE)

### Replace Lines 3661-3750 With:

```python
# dprompt-115 CORRECTED: Pattern-based trigger + LLM extraction
if req.is_correction and req.text:
    correction_applied = False
    
    # Step 1: Try each learned pattern as a trigger
    with db.cursor() as _cor_cur:
        _cor_cur.execute("""
            SELECT pattern, confidence, applicable_rel_types
            FROM correction_signals
            WHERE user_id = %s
            ORDER BY confidence DESC
            LIMIT 20
        """, (req.user_id,))
        patterns = _cor_cur.fetchall()
    
    for pattern_str, pattern_confidence, applicable_rel_types in patterns:
        try:
            # Step 2: Pattern-based TRIGGER (dumb)
            if not re.search(pattern_str, req.text, re.IGNORECASE):
                continue  # Pattern doesn't match
            
            log.info("correction_pattern_triggered",
                    pattern=pattern_str[:50],
                    confidence=pattern_confidence)
            
            # Step 3: Confidence GATE (dumb threshold)
            MIN_PATTERN_CONFIDENCE = 0.5
            if pattern_confidence < MIN_PATTERN_CONFIDENCE:
                log.info("correction_pattern_low_confidence",
                        pattern=pattern_str[:50],
                        confidence=pattern_confidence)
                continue  # Skip low-confidence pattern
            
            # Step 4: Smart extraction (LLM)
            llm_url = _get_llm_url()
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(
                        f"{llm_url}/v1/chat/completions",
                        json={
                            "model": os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                            "messages": [{
                                "role": "user",
                                "content": f"""Extract the correction from this user message:
                                
"{req.text}"

Return ONLY valid JSON (no markdown, no extra text):
{{
  "entity_name": "the person/thing being corrected (e.g. bob)",
  "attribute": "what property is corrected (e.g. age, height, weight, occupation)",
  "old_value": "the previous value (e.g. 10)",
  "new_value": "the corrected value (e.g. 11)",
  "confidence": 0.95
}}

If you cannot extract all fields, return {{"error": "reason"}}"""
                            }],
                            "temperature": 0.1
                        }
                    )
                
                if response.status_code != 200:
                    log.warning("correction_llm_error",
                               status=response.status_code)
                    continue
                
                extraction_text = response.json()["choices"][0]["message"]["content"]
                # Remove markdown code blocks if present
                extraction_text = extraction_text.replace("```json", "").replace("```", "")
                extraction = json.loads(extraction_text)
                
            except json.JSONDecodeError:
                log.warning("correction_extraction_invalid_json",
                           response=extraction_text[:100])
                continue
            except Exception as llm_error:
                log.warning("correction_llm_call_failed",
                           error=str(llm_error))
                continue
            
            # Step 5: Validate extraction
            if "error" in extraction:
                log.info("correction_extraction_returned_error",
                        error=extraction["error"])
                continue
            
            required_fields = ["entity_name", "attribute", "old_value", "new_value", "confidence"]
            if not all(f in extraction for f in required_fields):
                log.warning("correction_extraction_missing_fields",
                           extraction=extraction)
                continue
            
            extraction_confidence = float(extraction.get("confidence", 0))
            if extraction_confidence < 0.6:
                log.info("correction_extraction_low_confidence",
                        confidence=extraction_confidence)
                continue
            
            # Step 6: Resolve entity
            entity_name_lower = extraction["entity_name"].lower()
            resolved_entity = registry.resolve(req.user_id, entity_name_lower)
            if not resolved_entity:
                log.warning("correction_entity_not_found",
                           entity_name=extraction["entity_name"])
                continue
            
            # Step 7: Apply CLASS A correction
            success = await apply_scalar_correction_class_a(
                user_id=req.user_id,
                entity_id=resolved_entity,
                entity_name=extraction["entity_name"],
                attribute=extraction["attribute"].lower(),
                old_value=extraction["old_value"],
                new_value=extraction["new_value"],
                extraction_confidence=extraction_confidence,
                db=db
            )
            
            if success:
                log.info("correction_applied_class_a",
                        entity=extraction["entity_name"],
                        attribute=extraction["attribute"],
                        old_value=extraction["old_value"],
                        new_value=extraction["new_value"],
                        confidence=extraction_confidence)
                
                # Step 8: Learn (confirm pattern works, increase confidence)
                try:
                    with db.cursor() as _learn_cur:
                        _learn_cur.execute("""
                            UPDATE correction_signals
                            SET confidence = MIN(confidence + 0.05, 1.0),
                                applicable_rel_types = ARRAY_APPEND(
                                    COALESCE(applicable_rel_types, ARRAY[]::text[]),
                                    %s
                                ),
                                updated_at = now()
                            WHERE pattern = %s
                        """, (extraction["attribute"].lower(), pattern_str))
                        db.commit()
                except Exception as learn_error:
                    log.warning("correction_pattern_learning_failed",
                               error=str(learn_error))
                
                correction_applied = True
                break  # Success → stop trying patterns
            else:
                log.error("correction_database_update_failed",
                         entity=extraction["entity_name"],
                         attribute=extraction["attribute"])
                continue  # Try next pattern
        
        except Exception as pattern_error:
            log.warning("correction_pattern_error",
                       pattern=pattern_str[:50],
                       error=str(pattern_error))
            continue
    
    if not correction_applied:
        log.info("correction_not_applied_via_patterns",
                reason="no_pattern_matched_or_extraction_failed")
        # Fall through to normal extraction (graceful degradation)
```

---

## RECOMMENDATION 2: Implement apply_scalar_correction_class_a()

```python
async def apply_scalar_correction_class_a(
    user_id: str,
    entity_id: str,
    entity_name: str,
    attribute: str,
    old_value: str,
    new_value: str,
    extraction_confidence: float,
    db
) -> bool:
    """Apply user correction with full CLASS A semantics.
    
    User-stated facts are highest confidence, override globally,
    apply immediately, audit trail preserved.
    """
    # Validate rel_type is scalar
    if not _is_scalar_rel_type(attribute):
        log.warning("correction_not_scalar",
                   attribute=attribute)
        return False
    
    # Coerce new value
    val_text, val_int, val_float, val_date = _coerce_scalar(new_value)
    
    try:
        with db.cursor() as cur:
            # Mark old value as superseded (audit trail)
            cur.execute("""
                UPDATE entity_attributes
                SET valid_until = now()
                WHERE user_id = %s AND entity_id = %s 
                  AND attribute = %s AND valid_until IS NULL
            """, (user_id, entity_id, attribute))
            
            # INSERT or UPDATE new value (CLASS A with full metadata)
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
                    valid_from = now(),
                    valid_until = NULL,
                    updated_at = now()
            """, (user_id, entity_id, attribute, val_text, val_int, 
                  val_float, val_date, 'user_correction'))
            
            updated = cur.rowcount
            db.commit()
            
            return updated > 0
    
    except Exception as e:
        log.error("correction_apply_failed",
                 entity=entity_name,
                 attribute=attribute,
                 error=str(e))
        db.rollback()
        return False
```

---

## RECOMMENDATION 3: Update Pattern Storage to Track Rel_types

**Lines 3645-3660 (dprompt-114 pattern discovery):**

```python
# Already works, but improve to:
for pattern_info in patterns_detected:
    pattern = pattern_info["pattern"]
    pattern_type = pattern_info["pattern_type"]
    
    # Try to infer applicable rel_types from text
    inferred_rel_types = infer_rel_types_from_text(req.text)
    # Returns: ["age"] if "years old" or "years" in text, etc.
    
    cur.execute("""
        INSERT INTO correction_signals
        (pattern, pattern_type, priority, confidence, example_usage, 
         applicable_rel_types, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (pattern) DO UPDATE SET
            applicable_rel_types = ARRAY_CAT(
                COALESCE(correction_signals.applicable_rel_types, ARRAY[]::text[]),
                EXCLUDED.applicable_rel_types
            )
    """, (pattern, pattern_type, priority, confidence, 
          example_usage, inferred_rel_types))
```

---

## RECOMMENDATION 4: Use applicable_rel_types as Hints

**In correction extraction:**

```python
# After pattern matches, before LLM call:
applicable_rel_types = applicable_rel_types or []

if applicable_rel_types:
    # Use learned rel_types as hints to LLM
    rel_type_hint = f"Likely attributes: {', '.join(applicable_rel_types)}"
else:
    rel_type_hint = "Attribute is one of: age, height, weight, occupation"

# Include in LLM prompt:
"""
...
""" + rel_type_hint + """
...
"""
```

---

## RECOMMENDATION 5: Validate Corrections Improve Confidence

**After successful correction:**

```python
# Pattern confidence increases when:
# 1. Pattern matches
# 2. LLM extracts successfully
# 3. Database UPDATE succeeds

# Result: correction_signals.confidence increases over time
# System learns which patterns are most reliable
```

---

## FINDING CLARIFIED: The Architecture is Elegant

### What's Actually Happening (Correct Understanding)

```
Pattern storage (dprompt-114): Learn which patterns signal corrections
        ↓
Pattern triggering (simple regex): Does this text match pattern?
        ↓
Confidence gating (threshold check): Should we trust this pattern?
        ↓
Smart extraction (LLM): What exactly changed? (entity, attribute, values)
        ↓
CLASS A application (database): Update with provenance, audit trail
        ↓
Pattern learning (feedback loop): This pattern worked! Increase confidence
```

### What's Currently Broken

```
Pattern matching ✅
Confidence gating ❌ (tries to infer rel_type instead of gate)
Smart extraction ❌ (tries regex instead of LLM)
CLASS A application ❌ (missing audit trail, wrong confidence source)
Pattern learning ❌ (no feedback loop)
```

### The Fix

**Replace capture-group extraction with LLM extraction.**

That's it. Everything else follows.

---

## Impact Assessment

### Current State (Broken)
- ❌ 100% silent failure
- ❌ User sees "updated" but DB unchanged
- ❌ Pattern learning pointless (learned patterns never used)
- ❌ System unreliable

### After Fix (Elegant)
- ✅ Pattern triggers → LLM extracts → database updates
- ✅ System learns which patterns work
- ✅ Confidence increases over time
- ✅ Class A semantics respected (user is source of truth)
- ✅ Graceful fallback if pattern/extraction fails

---

## Implementation Priority

1. **CRITICAL:** Implement LLM extraction (Recommendation 1)
   - Remove capture-group logic
   - Add LLM call for extraction
   - Add validation + logging at each step

2. **CRITICAL:** Implement apply_scalar_correction_class_a() (Recommendation 2)
   - Full audit trail (provenance, valid_from/until)
   - Confidence from LLM, not pattern
   - Supersession tracking

3. **IMPORTANT:** Enable pattern learning (Recommendation 3-5)
   - Track applicable_rel_types
   - Increase confidence on success
   - Use hints in LLM prompt

4. **FOLLOW-UP:** Add /retract support for entity_attributes
   - Use valid_until filtering
   - Archive model queries

---

## Test Cases

### Test 1: Basic Age Correction
```
Input: "Actually, bob is 11, not 10"

Step 1: Pattern "is .+ not" matches ✅
Step 2: Confidence 0.8 > threshold 0.5 ✅
Step 3: LLM extracts:
  {
    "entity_name": "bob",
    "attribute": "age",
    "old_value": "10",
    "new_value": "11",
    "confidence": 0.95
  } ✅
Step 4: Database UPDATE with provenance='user_correction' ✅
Step 5: Pattern confidence increased: 0.8 → 0.85 ✅
Step 6: Next query returns age=11 ✅
```

### Test 2: Low-Confidence Extraction
```
Input: "Uh, I think bob's age is 11 not 10?"

Step 1: Pattern matches ✅
Step 2: Confidence gate passes ✅
Step 3: LLM extracts with confidence=0.45 ❌
Step 4: Skip this extraction (confidence < 0.6)
Step 5: Try next pattern (graceful degradation)
```

### Test 3: Pattern Learning
```
Input 1: "Actually, alice is 12, not 11" → confidence increases
Input 2: "Actually, charlie is 19, not 18" → confidence increases
Input 3: "Actually, I'm 5'10, not 5'9" → confidence increases

Result: Pattern "is .+ not" confidence: 0.8 → 0.85 → 0.90 → 0.95
        Pattern learns: applicable_rel_types = ["age", "height", ...]
```

---

## Summary: Smart Ingest, Dumb Extraction

The architecture is correct. The implementation is broken.

**Patterns are TRIGGERS, not extractors.**
- Trigger: "is there a correction?" (simple regex)
- Gate: "should we trust it?" (confidence threshold)
- Extract: "what changed?" (LLM)
- Learn: "did it work?" (confidence increase)

This is elegant because:
- ✅ Patterns grow via dprompt-114 (learn new patterns)
- ✅ Extraction is robust (LLM, not regex)
- ✅ System improves over time (confidence feedback)
- ✅ User is source of truth (CLASS A semantics)

**Fix: One change.** Replace regex extraction with LLM extraction.
