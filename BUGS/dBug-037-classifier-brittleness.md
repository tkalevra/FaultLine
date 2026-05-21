# dBug-037: Scalar Classification Brittleness — Metadata Ignored

**Status:** INVESTIGATION COMPLETE  
**Severity:** HIGH — Silent data corruption, constraint violation  
**Root Cause:** Heuristic-based classification ignores available metadata  
**Filed:** 2026-05-16

---

## Problem Summary

The ingest pipeline classifies facts as "scalar" or "relationship" using **heuristic pattern-matching on object values** (line 2121-2220 `classify_fact_type()`), **completely ignoring available metadata** in the `rel_types` table. This causes:

1. **Silent scalar→UUID corruption:** Scalar facts stored with UUID objects (violates hard constraint)
2. **Brittle classification:** Same value classified differently depending on pattern matches
3. **Unmaintainable:** New rel_types added to ontology don't auto-enforce constraints
4. **Metadata ignored:** `rel_types.tail_types = {SCALAR}` exists but never consulted

---

## Example: "My son alice is 12"

**Expected flow (metadata-driven):**
1. Extract: `(alice, age, "12")`
2. Ingest: Look up `age` in rel_types → `tail_types = {SCALAR}`
3. Store: `entity_attributes` (STRING value), not `facts` (UUID)
4. Result: ✓ Constraint enforced

**Actual flow (heuristic-driven):**
1. Extract: `(alice, age, "12")`
2. Ingest: `classify_fact_type("age", "12")` 
   - L1 check: `"12"` matches `^\d+$` → return `{"type": "scalar"}`
3. Store: `entity_attributes` (correct by accident)
4. Result: ✓ Works, but only because "12" happened to match pattern

**Why this is brittle:**
- If extraction produces `(alice, age, "alice_age_uuid")` → L3 UUID pattern → `{"type": "relationship"}` ✗ WRONG
- If extraction produces `(alice, age, "2026-05-16")` → L2 date pattern → `{"type": "scalar"}` ✓ Right answer, wrong reason
- If extraction produces `(alice, age, "twelve")` → L7 fallback uncertain → May be staged as B/C ✗ WRONG

---

## Metadata Available But Unused

**`rel_types` table has:**
```sql
SELECT rel_type, head_types, tail_types, storage_target, fact_class
FROM rel_types
WHERE rel_type IN ('age', 'spouse', 'pref_name', 'parent_of');
```

**Results:**
```
rel_type    | head_types | tail_types          | storage_target | fact_class
------------|------------|---------------------|----------------|----------
age         | {Person}   | {SCALAR}            | facts          | A
spouse      | {Person}   | {Person}            | facts          | A
pref_name   | ANY        | {SCALAR}            | facts          | A
parent_of   | {Person}   | {Person}            | facts          | A
```

**Constraint definition:**
- `tail_types = {SCALAR}` → object must be STRING (stored in `entity_attributes`)
- `tail_types = {Person}` → object must be Person UUID (stored in `facts`)
- `tail_types = {ANY}` → object can be anything

**Currently:** `_build_rel_type_meta()` loads only `category`, ignores `tail_types` (line 270-282).

---

## Code Paths

### 1. `classify_fact_type()` (line 2121–2220)
**Problem:** Uses 7 layers of heuristics (L0–L7), never checks rel_type metadata.

**Current order:**
- L0: `same_as` hardcoded → relationship
- L1: Numeric patterns → scalar (confidence 0.98)
- L2: Date patterns → scalar (confidence 0.90)
- L3: UUID pattern → relationship (confidence 0.98)
- L4: Entity alias lookup → relationship
- L5: Email/URL/phone heuristics → mixed
- L6: **rel_types.tail_types lookup** → mixed (NEVER REACHED if earlier match)
- L7: Fallback → uncertain

**Issue:** L6 consults the DB but only as a fallback (line 2139, 2200+). L0–L5 match first, preventing L6 from ever executing for known rel_types.

### 2. Metadata loading (line 1331)
```python
_REL_TYPE_META = _build_rel_type_meta(dsn)
```
**Problem:** Only loads `category`, not `tail_types`.

**Fix location:** Should expand to load:
```python
SELECT rel_type, category, head_types, tail_types, storage_target, fact_class
```

### 3. Constraint enforcement (multiple paths)
- Line 2666: Skip `registry.resolve()` for scalar rels ✓ (uses `_SCALAR_OBJECT_RELS`)
- Line 2674: Record display name only for non-scalar rels ✓
- Line 2692: Skip entity type updates for scalar rels ✓
- Line 2779: Skip user alias normalization for scalar rels ✓ (dprompt-95 guard added)
- Line 2869: Route scalar facts to `entity_attributes` ✓ (IF classifier says "scalar")

**Dependency chain:** All downstream constraints depend on `classify_fact_type()` returning correct classification. If classifier fails, constraints fail silently.

---

## Why Heuristics Are Brittle

1. **Pattern match conflicts:** Same value can match multiple patterns. "2026" matches year (L2) AND could be mistaken for other patterns.
2. **No single source of truth:** Classifier doesn't consult ontology. Two systems making independent decisions about same fact.
3. **Fallback is too late:** By the time L6 checks DB metadata, 99% of real-world values have already matched L1–L5.
4. **New rel_types ignored:** If LLM invents `"height_in_cm"`, classifier has no way to know it's scalar — must wait for human approval, then update CLAUDE.md constants.
5. **Extract garbage problem:** If extraction produces nonsense values, classifier might misclassify. Example: LLM tries to output age as UUID → classifier thinks it's relationship rel_type.

---

## "Strong Ingest, Dumb Extract" Principle

**Current state:** Ingest is dumb (heuristic-guessing), extract can be smart.
**Should be:** Ingest is strong (metadata-driven), extract is dumb (just produce edges).

**Why it matters:**
- Extract fires 1000x, Ingest fires 1000x — don't move logic to extract
- Ingest has database access, extract doesn't — use it
- Constraints are backend responsibility, not extract responsibility
- User corrections are ingest responsibility — metadata enforcement must be bulletproof

---

## Resolution System Not Leveraged

From user context: system has multi-source resolution:
- **User-grounded:** "me" → user UUID
- **Scope-aware:** "about" → scope query
- **Hierarchical:** "family" → taxonomy hierarchy
- **Entity resolution:** word → entity or value

**Missing:** Classifier doesn't use these. Should ask:
- "Is this rel_type known?" → consult ontology
- "What are valid objects for this rel_type?" → consult tail_types
- "Should this be stored as entity or attribute?" → consult storage_target

---

## Test Case: Demonstrating Brittleness

**Setup:** Insert age fact with value that doesn't match numeric patterns:

```bash
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-..." \
  -d '{
    "model": "qwen/qwen3.5-9b",
    "messages": [{"role": "user", "content": "My son alice is approximately twelve years"}]
  }'
```

**Prediction:**
- Classifier sees "approximately twelve years" (not purely numeric)
- L1 fails (not `^\d+$`)
- L2 fails (no date pattern)
- L3 fails (not UUID)
- L4 may match if "twelve" is somehow an alias
- L5 might match "long text" heuristic
- L6 fallback might eventually check tail_types
- Result: Uncertain classification → staged as Class C, not Class A ✗ WRONG

**If metadata-driven (fixed):**
- Look up "age" → `tail_types = {SCALAR}`
- confidence = 1.0, type = "scalar"
- Coerce "approximately twelve years" → value_int = 12
- Store in `entity_attributes` ✓

---

## Constraint Violations Enabled By Brittleness

1. **dBug-036A:** Scalar objects stored as UUIDs (line 2779 bypass when classifier misclassifies)
2. **User corrections ignored:** If classifier says "relationship" for an age fact, it never reaches entity_attributes correction path
3. **Type mismatches:** Age fact with non-numeric object should reject, but classifier might accept
4. **Silent data corruption:** No error, just wrong schema

---

## Fix Required (High-Level)

1. **Expand metadata loading:** Load `tail_types` into `_REL_TYPE_META`
2. **Move metadata check to L0:** Before any heuristics, consult ontology
3. **Deterministic fallback:** Only use heuristics for unknown rel_types (engine-generated)
4. **Validation:** Assert tail_types match classified type before storage

---

## Related Issues

- **dBug-036A:** Scalar objects converted to UUID (symptom of classifier failure)
- **dBug-035:** User spouse not ingested (symptom of classifier routing to wrong path)
- **CLAUDE.md constraint:** "Scalar rel_types have STRING objects" — not enforced in classifier

---

## Files Involved

| File | Lines | Issue |
|---|---|---|
| `src/api/main.py` | 270–282 | `_build_rel_type_meta()` loads only category, not tail_types |
| `src/api/main.py` | 2121–2220 | `classify_fact_type()` heuristic-only, metadata as fallback |
| `src/api/main.py` | 2855–2857 | Calls classifier, no validation of result |
| `src/api/main.py` | 2869–2975 | Routes based on classifier result (dependency) |
| `/migrations/022_rel_types_metadata.sql` | — | tail_types defined but unused in ingest |

