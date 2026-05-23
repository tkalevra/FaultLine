# dBug-HASPET-EXTRACTION: has_pet rel_type extraction inconsistent / hardcoded logic

**Status**: OPEN  
**Severity**: HIGH  
**Component**: Ingest Pipeline (extraction, validation)  
**Date Reported**: 2026-05-22  
**Assignee**: TBD  

---

## Summary

`has_pet` relationships are **not being extracted** even when:
1. Entities are mentioned in pet context ("We have a dog named Fraggle")
2. Entity types are correct (pet typed as Animal)
3. Other rel_types extract successfully in same messages
4. Type constraints are satisfied (Animal object type meets has_pet requirements)

**Suspected Root Cause**: Hardcoded validation logic, `has_pet`-specific extraction handling, or rel_type filtering that contradicts metadata-driven architecture (CLAUDE.md Principle: "Validation is metadata-driven").

---

## Evidence

### Test Case 1: Direct pet assertion
```
User: "We have a dog named Fraggle. He is a morkie."
Expected: (user, has_pet, fraggle), (fraggle, instance_of, morkie), (morkie, instance_of, dog)
Actual: instance_of facts created ✓, has_pet facts NOT created ✗
Database: 0 has_pet facts in facts table, 0 in staged_facts
```

### Test Case 2: Pet ingest with growth layer correction
```
User: "We got a new pet. His name is Biscuit. He is a golden retriever mix."
Expected: (user, has_pet, biscuit) at Class B (0.8 confidence)
Actual: 
  - Biscuit entity created ✓
  - Biscuit typed Animal (semantic correction) ✓
  - instance_of facts created ✓
  - has_pet facts: 0 ✗
```

### Test Case 3: Pet in multi-domain message
```
User: "I work for TechCorp. We have a office dog. Her name is Bella."
Expected: (user, has_pet, bella), (techcorp, has_pet?, bella)
Actual: All has_pet facts absent
Other facts: works_for created, office typed as Location (wrong), no has_pet
```

### Validation Chain: all constraints MET, yet no has_pet

```sql
-- Verify constraints are satisfied
SELECT e.id, e.entity_type, ea.alias
FROM entities e
LEFT JOIN entity_aliases ea ON e.id = ea.entity_id AND ea.is_preferred = true
WHERE ea.alias = 'biscuit'
AND e.user_id = '${TEST_USER_ID}';

-- Result: entity_type = Animal (PASSES has_pet head_types constraint)
```

has_pet rel_type metadata:
```
rel_type: has_pet
head_types: {Person}
tail_types: {Animal}
is_symmetric: false
is_hierarchy_rel: false
fact_class: B (Class B, staged)
```

**Constraint Check**: 
- Subject = user (Person) ✓
- Object = biscuit (Animal after type correction) ✓
- Confidence = 0.8 (Class B) ✓
- Yet: ZERO has_pet facts appear

---

## CONFIRMED: Extensive Hardcoding Found

### 🔴 CRITICAL: Hardcoded has_pet check with type case mismatch
**Location**: `src/wgm/gate.py:584`
```python
if rel_type == "has_pet" and object_type and object_type != "unknown" and object_type != "animal":
    log.warning("wgm.category_household_invalid_pet_type",
               rel_type=rel_type, object_type=object_type)
```

**Problem**:
- Line 1: `rel_type == "has_pet"` — HARDCODED comparison (should use metadata)
- Line 1: Checks `object_type != "animal"` (lowercase)
- System produces `object_type = "Animal"` (capitalized)
- **Case mismatch causes false warnings** (lowercase doesn't match uppercase)

**Violates CLAUDE.md**:
> "Validation is metadata-driven via `rel_types` table... no hardcoded validation constants."

### 🔴 Hardcoded entity type lists
**Location**: `src/wgm/gate.py:562-584`
```python
if subject_type and subject_type != "unknown" and subject_type != "person":  # line 562
if object_type and object_type != "unknown" and object_type != "location":  # line 573
if rel_type == "has_pet" and ... object_type != "animal":  # line 584
```

**Problem**: Entity type constraints hardcoded as string literals instead of queried from `rel_types.head_types`/`tail_types`

### 🔴 Hardcoded rel_type comparisons (40+ locations found)
**Examples**:
- `src/api/main.py:2348`: `if rel_type == "pref_name"`
- `src/api/main.py:2659`: `if rel in ("parent_of", "spouse", "has_pet")`  (hardcoded list)
- `src/api/main.py:4085`: `if e.rel_type == "parent_of"`
- `src/api/main.py:4090`: `if edge.rel_type == "child_of"`
- `src/wgm/gate.py:510`: `if rel_type == "instance_of"`
- `src/wgm/gate.py:542`: `if rel_type == "member_of"`

**Problem**: Logic should dispatch based on rel_type metadata, not hardcoded comparisons

### 🔴 Hardcoded category inference
**Location**: `src/api/main.py:260-270`
```python
def _infer_category_from_rel_type(rt: str) -> str:
    if any(k in rt for k in ("live","address","location","city","home","reside")):
        return "location"
    # ... more hardcoded keyword matching
    if any(k in rt for k in ("pet","animal","dog","cat","fish","bird")):
        return "pets"
```

**Problem**: Categories should come from `rel_types.category` metadata, not keyword inference

### 🔴 Hardcoded household/location relation sets
**Location**: `src/wgm/gate.py:579-588`
```python
household_rels = {"has_pet", "owns"}
if rel_type in household_rels:
    ...
```

**Problem**: Relation groupings should come from `entity_taxonomies.rel_types_defining_group`, not hardcoded sets

---

## Investigation Steps

### Step 1: Verify metadata is complete
```bash
ssh docker-host "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c \"
SELECT rel_type, head_types, tail_types, is_symmetric, is_hierarchy_rel, natural_language 
FROM rel_types 
WHERE rel_type IN ('has_pet', 'spouse', 'parent_of')
ORDER BY rel_type;
\""

# Expected: has_pet row with proper constraints
# If missing or NULL: metadata gap
```

### Step 2: Check extraction prompt includes has_pet
```bash
# Temporary debug: add logging to extraction prompt
# Search src/api/main.py for _build_extraction_prompt()
# Verify has_pet appears in generated prompt sent to LLM
```

### Step 3: Grep for hardcoded has_pet logic
```bash
grep -rn "has_pet" /home/${USER}/Documents/013-GIT/FaultLine-dev/src/ \
  --include="*.py" | grep -v "\.pyc" | grep -v "test" | sort

# Look for patterns like:
# - if "has_pet" in ...
# - rel_type == "has_pet"
# - "has_pet" not in list
# - hardcoded validation
```

### Step 4: Test extraction directly
```bash
# Call /extract/rewrite directly with pet message
curl -X POST "http://${BACKEND_IP}:8001/extract/rewrite" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user",
    "text": "We have a dog named Fluffy",
    "user_aliases": ["${USER}", "${USER}"]
  }' | jq '.extracted_edges[] | select(.rel_type == "has_pet")'

# If returns nothing: extraction isn't producing has_pet triples
# If returns has_pet: issue is in /ingest validation or staging
```

### Step 5: Check Filter scope filtering
```bash
# Verify has_pet is in household taxonomy and query scope detection includes it
SELECT * FROM entity_taxonomies WHERE taxonomy_name = 'household';

# If has_pet not in rel_types_defining_group: query scope filtering removes it
```

---

## Root Cause Hypothesis Ranking

1. **HIGHEST**: Extraction prompt doesn't include has_pet examples  
   - has_pet is relational but might not be in rel_types query for LLM
   - Easy to miss: only 7 example rels shown to LLM, has_pet might not make cut

2. **HIGH**: Hardcoded validation in WGMValidationGate  
   - Some rel_types get special validation not in metadata
   - has_pet type constraints might be enforced via code, not rel_types

3. **MEDIUM**: Class B staging logic filters has_pet  
   - Confidence assignment might downgrade has_pet to Class C (0.4, expires in 30 days)
   - Check confirmed_count logic

4. **MEDIUM**: Query scope filtering removes has_pet from /query results  
   - Even if facts exist in DB, query might filter them out
   - Filter doesn't request /query data if scope doesn't include has_pet

---

## CLAUDE.md Violations

This bug represents violations of core FaultLine principles:

1. **"Validation is metadata-driven"**  
   → If has_pet has hardcoded logic, this is violated

2. **"No hardcoded validation constants"**  
   → Search for literal "has_pet" comparisons in validation code

3. **"All rel_types self-describe their constraints"**  
   → has_pet constraints should come from rel_types table, not code

4. **"Generic/scope-agnostic, works for any rel_type"**  
   → If has_pet is hardcoded, it breaks generic ingest

---

## Proposed Fix

Once root cause is identified, fix must be:

1. **Metadata-driven**: Move any hardcoded has_pet logic to rel_types table
2. **Generic**: Solution applies to ALL rel_types, not just has_pet
3. **Non-brittle**: Add test case to prevent regression
4. **Traceable**: Log why facts aren't extracted (add DEBUG logging to extraction/validation)

---

## Testing Plan

After fix:
```bash
# Test 1: Direct extraction
curl ... /extract/rewrite ... "We have a dog" 
# Should return (user, has_pet, dog)

# Test 2: Full ingest
curl ... /ingest ... (user, has_pet, dog) edge
# Should stage in staged_facts as Class B (0.8)

# Test 3: Query returns has_pet
curl ... /query ... "pets"
# Should return has_pet facts

# Test 4: Cross-domain consistency
# Run same test for other rel_types (works_for, parent_of, etc.)
# All should work identically
```

---

## References

- CLAUDE.md: "Validation is metadata-driven via `rel_types` table"
- Commit 59dfffe: dprompt-127-Layer2 (type correction working, extraction not)
- Commit a6f8c50: Semantic-aware type inference (growth layer verified working)
- Test Results: has_pet facts exist in 0 cases despite entity types correct
