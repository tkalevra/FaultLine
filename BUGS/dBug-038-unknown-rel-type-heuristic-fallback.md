# dBug-038: Unknown rel_type Falls Back to Relational Classification

**Status:** IDENTIFIED  
**Severity:** MEDIUM — Incorrect routing of unknown rel_types  
**Root Cause:** Phase 1 (classify_fact_3d) heuristic fallback misclassifies unknown rel_types  
**Filed:** 2026-05-16

---

## Problem Summary

When LLM extraction produces an unknown rel_type (e.g., "age_of" instead of "age"), the ingest pipeline's Phase 1 classifier falls back to heuristics (L1–L5 value patterns) and routes to RELATIONAL by default. This causes:

1. **Wrong storage path:** Unknown rel_types routed to facts table (relational) when they should await re_embedder correction
2. **Blocking correction:** Once stored as relational, conflicting with downstream type constraints
3. **Re_embedder mappers ignored:** Re_embedder correctly identifies "age_of" → "age" mapping (score 0.938) but "rewritten=0" means no action taken

---

## Example: "My son alice is 12 years old"

**Extraction:** Produces `(alice, age_of, "12")` ← Wrong rel_type (should be "age")

**Ingest Phase 1:**
```
rel_type="age_of" (unknown)
  ↓
Check _REL_TYPE_META["age_of"] → NOT FOUND
  ↓
Fall through to L1–L5 heuristics
  ↓
L1: "12" matches ^\d+$ → return storage="scalar" ✓ CORRECT
  BUT...
L3: "12" could match UUID? No
L4: Alias lookup? No
L5: Email/URL? No
  ↓
FALLBACK: No pattern match → storage=None OR storage="relational" (DEFAULT)
```

**Current behavior:** Classified as relational (unknown rel_type default)

**Expected:** Either:
1. Reject unknown rel_types until re_embedder corrects them
2. Stage as Class C (0.4, awaits confirmation)
3. Block relational fallback; require metadata approval first

---

## Code Location

**File:** `src/api/main.py`  
**Function:** `classify_fact_3d()` (lines ~284–400)  
**Issue:** Lines 365–373 (L1–L5 fallback) and lines 375–378 (final FALLBACK)

```python
# Current: Falls through to relational without metadata approval
if re.match(r'^-?\d+$', stripped):
    return {"storage": "scalar", ...}  # Correctly identifies "12" as numeric

# ... but for unknown rel_type with no pattern match...
return {
    "storage": None,  # ← Returns None, but downstream code defaults to relational?
    "direction": None,
    "reason": "no pattern match, unknown rel_type",
}
```

---

## Root Cause

**aliceign assumption broken:** The 3D model assumes extraction produces **known rel_types** or values that match patterns. Unknown rel_types that DON'T match value patterns fall through with `storage=None`, but the downstream code (Phase 4 routing) may default to relational instead of rejecting.

**Flow:** 
1. Extract: LLM produces "age_of" (unknown)
2. Classify: Phase 1 can't find metadata, checks patterns
3. Pattern match: "12" is numeric (scalar pattern!) BUT rel_type="age_of" is unknown
4. **Conflict:** Value suggests scalar, but rel_type is unknown
5. **Current behavior:** Unknown rel_type wins → relational fallback (wrong!)

---

## Why Re_embedder Mapping Doesn't Fix It

The re_embedder runs asynchronously AFTER ingest stores the fact:

```
Ingest: Store (alice, age_of, "12") as relational → staged_facts
  ↓ (minutes later)
Re_embedder: ontology_evaluations find "age_of" → "age" mapping (score 0.938)
  ↓
Try to rewrite: rewritten=0 (NO ACTION)
  ↓
Result: Fact remains in wrong storage path (relational instead of scalar)
```

**The rewritten=0 indicates the re_embedder couldn't/didn't rewrite.** The fact is stuck in relational.

---

## Solution (Phase 1 Refinement)

### Option A: Strict Mode (Recommended)
If Phase 1 can't determine storage path with BOTH metadata AND value patterns, **reject** (return storage=None):

```python
# Phase 1: Metadata-first
if rel_meta:
    # Known rel_type → use metadata (CURRENT: works ✓)
    return {"storage": storage, ...}

# Phase 1b: Value-pattern fallback
if _matches_value_pattern(value):
    # Value clearly indicates scalar/relational
    return {"storage": inferred_from_value, ...}

# Phase 1c: Ambiguous
# Unknown rel_type AND no clear value pattern
return {
    "storage": None,
    "confidence": 0.0,
    "reason": "unknown rel_type with ambiguous value pattern",
}
```

Then Phase 4 routing:
```python
if not classification_3d["storage"]:
    log.warning("ingest.classification_rejected", rel_type=edge.rel_type)
    continue  # Skip this fact, await re_embedder correction
```

### Option B: Lenient Mode with Staging
Accept unknown rel_types but stage as Class C (0.4) for re_embedder evaluation:

```python
if not classification_3d["storage"]:
    # Ambiguous: stage for re_embedder evaluation
    fact_class, confidence = ("C", 0.4)
    _commit_staged([...], "C", 0.4)
```

---

## Test Case: Demonstrating Bug

**Input:** "My son alice is 12 years old"  
**LLM extracts:** `(alice, age_of, "12")`

**Current (WRONG):**
- Phase 1: storage=None (or relational fallback)
- Phase 4: Routes to facts (relational)
- Result: alice.age_of="12" in facts table (wrong schema)

**Expected (FIXED):**
- Phase 1: storage=None (ambiguous)
- Phase 4: Reject or stage as Class C
- Result: Await re_embedder → maps "age_of"→"age" → re_ingest with correct metadata

---

## Metadata Coverage

This bug only occurs when:
- rel_type is unknown (not in rel_types table) AND
- Value doesn't match a strong heuristic pattern (not purely numeric/UUID/email)

**Known rel_types never exhibit this:** "age" always routes to scalar (tail_types={SCALAR})

---

## Related Issues

- **dBug-037:** Classifier brittleness (heuristic-first aliceign)
- **dBug-036A:** Scalar objects converted to UUID (consequence of wrong routing)

---

## Recommendation

**Implement Option A (Strict Mode):**
1. Reject unknown rel_types with ambiguous values
2. Log as "classification_rejected" not "fact_classified"
3. Re_embedder will map/correct, then re_ingest with metadata
4. Prevents silent misrouting to wrong storage path

**Timeline:** Fix before running full test suite (prevents cascading failures)

