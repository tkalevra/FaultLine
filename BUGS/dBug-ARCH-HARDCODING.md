# dBug-ARCH-HARDCODING: Architectural Violation - 40+ Hardcoded Comparisons

**Status**: CRITICAL OPEN  
**Severity**: CRITICAL (Architecture)  
**Component**: Core ingest pipeline (WGM gate, ingest, extraction)  
**Date Reported**: 2026-05-22  
**Type**: Architectural Violation  

---

## Summary

**The codebase contains 40+ hardcoded rel_type and entity_type comparisons that directly violate CLAUDE.md architectural principles.**

FaultLine is designed as a **write-validated knowledge graph with metadata-driven validation**. Instead, the code is full of hardcoded decisions that should come from the database:

- `if rel_type == "has_pet"` ← hardcoded  
- `if rel_type in ("parent_of", "spouse", "has_pet")` ← hardcoded list  
- `if entity_type != "person"` and `object_type != "animal"` ← hardcoded types  
- `household_rels = {"has_pet", "owns"}` ← hardcoded relation group  

**This breaks scalability, growth, and the entire metadata-driven architecture.**

---

## CLAUDE.md Violations

The following principles are VIOLATED:

### Principle 1: "Validation is metadata-driven"
```
From CLAUDE.md:
"All validation is **metadata-driven** via `rel_types` table 
(`is_leaf_only`, `is_hierarchy_rel`, `inverse_rel_type`, `is_symmetric`). 
`_get_rel_type_metadata()` queries metadata at runtime — no hardcoded 
validation constants. New rel_types self-describe their constraints."
```

**VIOLATED**: 40+ locations hardcode rel_type checks and entity type constraints instead of querying metadata.

### Principle 2: "No hardcoded rel_types"
```
From CLAUDE.md constraint list:
"No name-based entity pre-creation"
"All entity_ids must be UUIDs"
"Validation is metadata-driven — rel_types table stores validation properties"
```

**VIOLATED**: Hardcoded lists like `("parent_of", "spouse", "has_pet")` prevent new rel_types from working without code changes.

### Principle 3: "Growth layer self-healing"
```
From CLAUDE.md:
"New rel_types self-describe their constraints without code changes."
```

**VIOLATED**: Adding a new rel_type (e.g., "mentors") requires:
1. Adding it to hardcoded comparison lists
2. Adding it to hardcoded category inference
3. Adding entity type validation
4. Adding special case handling

Instead of just adding a row to `rel_types` table.

---

## Evidence: Hardcoding Locations

### src/wgm/gate.py (Validation Gate)

**Line 584: has_pet-specific type check**
```python
if rel_type == "has_pet" and object_type and object_type != "unknown" and object_type != "animal":
    log.warning("wgm.category_household_invalid_pet_type", ...)
```
❌ Hardcoded rel_type comparison  
❌ Hardcoded entity type ("animal" lowercase, when system produces "Animal")  
❌ Not in metadata  

**Lines 562-588: Hardcoded entity type validation**
```python
if subject_type and subject_type != "unknown" and subject_type != "person":  # line 562
if object_type and object_type != "unknown" and object_type != "person":   # line 565
if object_type and object_type != "unknown" and object_type != "location": # line 573
if subject_type and subject_type != "unknown" and subject_type != "person": # line 581
if rel_type == "has_pet" and ... object_type != "animal":                  # line 584
```
❌ Entity type constraints hardcoded as string literals  
❌ Should query `rel_types.head_types` and `rel_types.tail_types`  
❌ Makes adding new entity types impossible without code changes  

**Line 579: Hardcoded relation group**
```python
household_rels = {"has_pet", "owns"}
if rel_type in household_rels:
```
❌ Relation groupings hardcoded instead of queried from `entity_taxonomies.rel_types_defining_group`  

### src/api/main.py (Extraction & Ingest)

**Lines 260-270: Hardcoded category inference**
```python
def _infer_category_from_rel_type(rt: str) -> str:
    if any(k in rt for k in ("live","address","location","city","home","reside")):
        return "location"
    if any(k in rt for k in ("parent","child","spouse","sibling","family","brother","sister")):
        return "family"
    if any(k in rt for k in ("pet","animal","dog","cat","fish","bird")):
        return "pets"
```
❌ Categories should come from `rel_types.category` metadata  
❌ Keyword inference fragile and error-prone  
❌ Hardcoded keywords won't match novel rel_types  

**Line 2348: pref_name-specific logic**
```python
if rel_type == "pref_name":
    ...
```

**Line 2659: Hardcoded rel_type list for context building**
```python
if rel in ("parent_of", "spouse", "has_pet"):
    profile_parts.append(f"{rel}={obj}")
```
❌ Every new rel_type needs code change to be included in context  

**Lines 2994, 4033, 4085, 4090, 4215, 4455, 4578, 4615, 4620, 4740, 4755, 5528, 5673, 5681, 6152, 6742, 7285**: 15+ more hardcoded comparisons

### src/extraction/compound.py

**Line 357-358: Hardcoded rel_type extraction**
```python
_pref_names: set[str] = {e["object"].lower() for e in edges if e["rel_type"] == "pref_name"}
_spouse_names: set[str] = {e["object"].lower() for e in edges if e["rel_type"] == "spouse"}
```
❌ Only handles specific rel_types  
❌ Other rel_types not extracted  

---

## Impact: Why This Breaks FaultLine

### 1. New rel_types require code changes
User adds new rel_type "mentors" to rel_types table.
Result: **Feature doesn't work until developer adds hardcoded handling.**

### 2. Entity type inference breaks
System infers "Animal" (capital) but code checks `!= "animal"` (lowercase).
Result: **Type constraints fail, facts rejected or incorrectly validated.**

### 3. Growth layer can't scale
dprompt-127 type correction works for pets but applies semantic patterns universally.
Result: **System can't learn new entity type inference rules without code changes.**

### 4. Query/Filter coupling breaks
Filter doesn't know which rel_types to request from backend.
Result: **New rel_types aren't injected to LLM even if facts exist.**

### 5. Category inference unreliable
Keyword-based inference fails for novel rel_types.
Result: **has_pet extraction fails despite all constraints met (lowercase vs uppercase).**

---

## Root Cause

**The code was built with assumption that rel_types are fixed and small set.**

When system grows (new rel_types, new entity types, new categories), hardcoding becomes a bottleneck.

CLAUDE.md mandates metadata-driven approach to enable growth, but code has regressed to hardcoding.

---

## Fix Required

**Replace ALL hardcoded comparisons with metadata queries.**

### Pattern 1: rel_type comparison
**BEFORE** (hardcoded):
```python
if rel_type == "has_pet":
    # special handling
```

**AFTER** (metadata-driven):
```python
rel_meta = _get_rel_type_metadata(db, rel_type)
if rel_meta.get("category") == "household":
    # handle all household rel_types generically
```

### Pattern 2: Entity type validation
**BEFORE** (hardcoded):
```python
if object_type != "unknown" and object_type != "animal":
    reject()
```

**AFTER** (metadata-driven):
```python
rel_meta = _get_rel_type_metadata(db, rel_type)
tail_types = rel_meta.get("tail_types", [])
if tail_types and object_type not in tail_types and object_type != "unknown":
    reject()
```

### Pattern 3: Relation grouping
**BEFORE** (hardcoded):
```python
household_rels = {"has_pet", "owns"}
if rel_type in household_rels:
```

**AFTER** (metadata-driven):
```python
with db.cursor() as cur:
    cur.execute(
        "SELECT rel_types_defining_group FROM entity_taxonomies WHERE taxonomy_name = %s",
        ("household",)
    )
    household_rels = set(cur.fetchone()[0])
if rel_type in household_rels:
```

---

## Investigation Checklist

- [ ] Identify ALL hardcoded rel_type comparisons (40+ found)
- [ ] Identify ALL hardcoded entity type strings
- [ ] Identify ALL hardcoded category lists
- [ ] Identify ALL hardcoded relation groupings
- [ ] Audit each location for metadata equivalent
- [ ] Verify rel_types table has all metadata columns
- [ ] Verify entity_taxonomies table has all required data
- [ ] Create migration to populate missing metadata
- [ ] Refactor code to remove hardcoding
- [ ] Add test: new rel_type works without code change
- [ ] Add test: new entity_type works without code change

---

## Timeline

This is a **BLOCKER for production**.

Hardcoding prevents:
- ✗ New rel_types from working
- ✗ Type inference from scaling
- ✗ Growth layer from learning new patterns
- ✗ has_pet extraction from working (case mismatch)

**Recommend**: Full audit + refactor before next production deployment.

---

## References

- CLAUDE.md: Architecture principles (lines marked "HARD CONSTRAINT")
- dBug-HASPET-EXTRACTION: Specific example of hardcoding failure
- Commits: 59dfffe, a6f8c50 (growth layer reveals hardcoding issues)
