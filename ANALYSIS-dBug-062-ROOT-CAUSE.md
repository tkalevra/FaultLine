# dBug-062 Root Cause Analysis: LLM Extraction Returns Rel_Type Names as Entity Values

**Date:** 2026-05-21  
**Status:** ROOT CAUSE IDENTIFIED  
**Severity:** CRITICAL

---

## Executive Summary

The LLM extraction is returning **rel_type names substituted for entity values** because the extraction prompt uses **ambiguous placeholders** in the FORMAT line that confuse the LLM about what should appear in each field.

---

## Root Cause

**File:** `src/api/main.py:3027`

**Problematic Prompt Line:**
```
FORMAT: [{"subject":"entity","object":"value","rel_type":"rel_type","definition":"short description"}]
```

**Problem:** The `"rel_type":"rel_type"` placeholder uses the same word as both key and value. When followed immediately by EXTRACT RULES that list actual rel_type names (pref_name, also_known_as, parent_of), the LLM becomes confused about whether:

1. `"rel_type"` is a placeholder (meaning "use the actual rel_type name")
2. `"rel_type"` is a literal value (meaning "the output should contain the string 'rel_type'")
3. The rel_type names listed in lines 3037-3044 should appear in the `object` field instead of `rel_type`

---

## Why This Causes dBug-062 Symptoms

**Test Input:** "My son ChildB is also known as ArtMajor, he is 19..."

**Expected Triple:** `{"subject":"ChildB","object":"ArtMajor","rel_type":"also_known_as","definition":"..."}`

**Actual Triple:** `{"subject":"ChildB","object":"pref_name","rel_type":"pref_name","definition":"..."}`

**LLM Logic (Reconstructed):**
1. Sees FORMAT line: `"rel_type":"rel_type"` → confuses placeholder with literal value
2. Reads EXTRACT RULES listing pref_name, also_known_as, instance_of, parent_of
3. When extracting identity facts, instead of asking "what is the object entity?", asks "what rel_type should I use?"
4. Outputs: `"object": "pref_name"` (the rel_type name) and `"rel_type": "pref_name"` (also the rel_type name)

---

## Evidence Chain

### Symptom 1: Rel_Type Names in Object Field
```json
{
  "subject": "ChildB",
  "object": "pref_name",        // ❌ Should be "ChildB"
  "rel_type": "pref_name"
}
```

### Symptom 2: Age Stored as Rel_Type Name
```
ingest.age_rejected_non_numeric_object: object=age subject=child_b
                                                   // ❌ Should be "19", not "age"
```

### Symptom 3: Rel_Type Names Treated as Entities in Database
```
[info] ingest.pref_name_injected: entity=also_known_as
                                          // ❌ "also_known_as" treated as entity name
```

### Symptom 4: False Entity Creation
Facts show `pref_name` and `also_known_as` being resolved as UUID entities instead of being recognized as rel_type names.

---

## Why dprompt-126 Layer 1 Doesn't Catch This

Hierarchy membership validation (Layer 1) can't prevent this because:
1. **False entities are created at extraction time** — before validation gates run
2. `pref_name`, `also_known_as`, `parent_of` are created as valid Person entities (happens to match type constraints)
3. Validation sees: Person → pref_name (Person) → valid hierarchy membership
4. **No semantic validation** checks "wait, is 'pref_name' actually an entity or a rel_type name?"

This is an **extraction-layer problem**, not a validation-layer problem.

---

## The Fix: Disambiguate the FORMAT Line

**Current (Ambiguous):**
```
FORMAT: [{"subject":"entity","object":"value","rel_type":"rel_type","definition":"short description"}]
```

**Proposed (Clear with Actual Examples):**
```
FORMAT: [
  {"subject":"ChildB","object":"ArtMajor","rel_type":"also_known_as","definition":"ChildB is also known as ArtMajor"},
  {"subject":"ChildB","object":"19","rel_type":"age","definition":"ChildB is 19 years old"},
  {"subject":"user","object":"ChildB","rel_type":"parent_of","definition":"User is the parent of ChildB"},
  {"subject":"ChildB","object":"ArtMajor Major","rel_type":"occupation","definition":"ChildB is an ArtMajor Major"}
]
```

**Why This Works:**
- Concrete examples show the LLM exactly what output should look like
- No ambiguous placeholder `"rel_type":"rel_type"`
- Demonstrates:
  - Identity facts: object is entity name, rel_type is also_known_as/pref_name
  - Scalar facts: object is a value (19, "ArtMajor Major"), rel_type is age/occupation
  - Relationship facts: both subject and object are entity names
- Multiple examples cover all three patterns the LLM needs to learn

---

## Secondary Issue: Rel_Type Names Listed in EXTRACT RULES

Lines 3037-3044 list rel_type names in a context that could reinforce the confusion:

```python
1. Identity: pref_name, also_known_as, same_as (pronouns → entities)
2. Entity types: instance_of for EVERY entity...
3. Hierarchies: For locations...
4. Family kinship (CRITICAL):
   - "My children are Gabby, Des" → (user, parent_of, gabby), (user, parent_of, des)
   - "My son's name is X" → (user, parent_of, x), THEN (x, pref_name, x)  ← rel_type name appears here
```

The problem: `(x, pref_name, x)` shows the rel_type name `pref_name` between entity names `x` and `x`. When this follows immediately after the FORMAT line with `"rel_type":"rel_type"`, the LLM's pattern-matching sees rel_type names in example triples and might generalize "rel_type names can appear as object values."

**Secondary Fix:** Rewrite examples to be more explicit:

```python
4. Family kinship (CRITICAL):
   - "My children are Gabby, Des" → 
     (user, parent_of, gabby), (user, parent_of, des)
   - "My son's name is X" — extract TWO triples:
     FIRST: (user, parent_of, x) — X is a CHILD entity
     SECOND: (x, pref_name, "x's actual name") — NOW extract the child's preferred name
```

This makes clear that when extracting `(x, pref_name, ???)`, the object should be "x's actual name" (a string value), NOT the rel_type name "pref_name".

---

## Implementation Plan

### Phase 1: Immediate Fix (FORMAT Line Clarification)
Replace line 3027 with concrete examples showing all three patterns:
1. Identity facts (object = entity name, rel_type = pref_name/also_known_as)
2. Scalar facts (object = value string, rel_type = age/occupation)
3. Relationship facts (object = entity name, rel_type = parent_of/spouse)

### Phase 2: Refinement (EXTRACT RULES Clarity)
Rewrite lines 3040-3044 to be more explicit about what the object should be in each pattern.

### Phase 3: Validation Layer Enhancement
Add a sanity check in `_validate_triple_against_metadata()` (line 3272) to detect when a rel_type name appears in the object field and flag it as an extraction error.

---

## Testing Verification

After fix, test with:
```
Message: "My son ChildB is also known as ArtMajor, he is 19 and an ArtMajor Major."

Expected triples:
✓ {"subject":"ChildB","object":"ArtMajor","rel_type":"also_known_as"}
✓ {"subject":"ChildB","object":"19","rel_type":"age"}
✓ {"subject":"ChildB","object":"ArtMajor Major","rel_type":"occupation"}
✓ {"subject":"user","object":"ChildB","rel_type":"parent_of"}

Verify NO triples with:
✗ "object":"pref_name"
✗ "object":"also_known_as"
✗ "object":"age"
✗ "object":"parent_of"
```

---

## Impact

**Fixes:**
- dBug-062 (extraction rel_type corruption)
- dBug-art-false-children (false entity creation from extraction)
- Improves extraction accuracy across ALL rel_types with similar pattern issues

**Risk:** LOW — prompt change only, no code logic changes needed (can iterate quickly)

**Timeline:** 15 minutes to implement + deploy (function-only, no rebuild needed)
