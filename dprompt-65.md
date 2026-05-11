# dprompt-65: Metadata-Driven Validation Framework (Unified, Dynamic, No Technical Debt)

**Date:** 2026-05-13  
**Severity:** Architecture — prevents cascading bugs from dynamic ontology  
**Status:** Specification ready for implementation  

## Problem

Current validation approach (dprompt-58/59/62) uses hardcoded rules:
- Extraction constraint: hardcoded list of leaf-only rels (owns, has_pet, works_for, etc.)
- Conflict detection: hardcoded checks for hierarchy objects
- Bidirectional validation: hardcoded inverse pairs (parent_of ↔ child_of)

**Fragility:** As LLM creates new rel_types and hierarchy rels dynamically, validation rules become outdated. Requires new dprompt for each edge case (dprompt-63, dprompt-64, etc.). Technical debt accumulates.

**Root cause:** Validation rules are hardcoded; ontology is dynamic. They don't match.

## Solution: Metadata-Driven Validation

Store validation properties IN the ontology itself. When LLM creates a new rel_type, it defines:
- Is it symmetric? (spouse ↔ spouse, or spouse ≠ spouse?)
- What's its inverse? (parent_of ↔ child_of)
- Is it leaf-only? (can it apply to type entities? owns can't, has_pet can't)
- Is it a hierarchy rel? (instance_of, subclass_of, member_of, part_of, is_a)
- What rel_types can apply to its objects? (if instance_of, only certain rels allowed)

Validation framework queries metadata at runtime. **New rel_types self-describe their validation rules.**

## Implementation Overview

### Part A: Expand rel_types Table Schema

Add validation metadata columns:
```sql
ALTER TABLE rel_types ADD COLUMN (
  is_symmetric BOOLEAN DEFAULT FALSE,           -- spouse, knows, friend_of, met
  inverse_rel_type VARCHAR(100) DEFAULT NULL,   -- parent_of → child_of
  is_leaf_only BOOLEAN DEFAULT FALSE,           -- cannot apply to hierarchy objects
  is_hierarchy_rel BOOLEAN DEFAULT FALSE,       -- instance_of, subclass_of, part_of, etc.
  allows_leaf_rels TEXT[] DEFAULT NULL          -- if hierarchy, which rels can apply to objects
);
```

**Pre-populate for existing rel_types:**
```
parent_of: is_symmetric=false, inverse=child_of, is_leaf_only=true
spouse: is_symmetric=true, inverse=null, is_leaf_only=false
instance_of: is_symmetric=false, inverse=null, is_leaf_only=false, is_hierarchy=true, allows_leaf_rels=['has_pet', 'owns', 'works_for']
```

### Part B: LLM Populates Metadata on Novel Extraction

When LLM extracts or creates a new rel_type (e.g., "supervises"):
1. Extract rel_type name + confidence
2. **Also extract:** is it symmetric? inverse_rel_type? leaf_only? hierarchy?
3. Store in `ontology_evaluations` or directly in `rel_types` (if approved)

Example flow:
```
LLM extracts: "alice supervises bob" + rel_type="supervises"
LLM also provides: supervises { is_symmetric: false, inverse: "supervised_by", is_leaf_only: true }
System stores both in ontology (fact + metadata)
```

### Part C: Unified Validation Framework

Replace hardcoded rules with metadata queries:

```python
def _validate_fact(subject_id, rel_type, object_id, context="ingest"):
  rel_metadata = query_rel_types(rel_type)
  
  # Check 1: Leaf-only rels cannot apply to hierarchy objects
  if rel_metadata.is_leaf_only:
    if is_hierarchy_object(object_id):  # is it object of instance_of/subclass_of/etc.?
      return CONFLICT("leaf_only_violation", reason=f"{rel_type} cannot apply to type entity")
  
  # Check 2: Bidirectional validation (inverse pairs)
  if rel_metadata.inverse_rel_type:
    inverse_facts = query_facts(subject_id, rel_metadata.inverse_rel_type, object_id)
    if inverse_facts:
      return CONFLICT("bidirectional_conflict", keep_higher_confidence)
  
  # Check 3: Symmetric rels allowed bidirectional
  if rel_metadata.is_symmetric:
    return VALID()  # both directions expected
  
  # Check 4: Hierarchy-specific validation
  if rel_metadata.is_hierarchy_rel:
    # Ensure proper transitivity, no cycles, etc.
    return validate_hierarchy_semantics()
  
  return VALID()
```

Apply this framework uniformly to:
- Facts table (Class A ingest)
- Staged_facts (Class B unconfirmed)
- Qdrant upserts

## Scope & Integration

**Does NOT change:**
- Fact storage schema (only expands rel_types)
- Extraction pipeline (LLM provides metadata, system stores it)
- Query/retrieval logic

**Does change:**
- `/ingest` validation to query rel_type metadata instead of hardcoded rules
- `rel_types` table schema (add metadata columns)
- Ontology storage (track rel_type properties alongside creation)

**Applies to:**
- Existing hardcoded rel_types (populate metadata once)
- Future LLM-created rel_types (LLM provides metadata on creation)

## Example: New Rel_Type Creation

**Before (hardcoded):**
```
LLM creates "supervises" rel_type → validation has no rules for it → defaults to permissive
Result: "alice supervises engineer_role" allowed (wrong)
Need dprompt-63 to add rules for "supervises"
```

**After (metadata-driven):**
```
LLM extracts: "alice supervises bob"
  rel_type="supervises"
  metadata: { is_symmetric: false, inverse: "supervised_by", is_leaf_only: true }

System stores both.

Validation checks:
  - "supervises" is leaf_only → reject if object is hierarchy target
  - if inverse "supervised_by" exists for same pair → keep higher confidence
  
Result: "alice supervises engineer_role" auto-rejected (correct)
NO NEW DPROMPT NEEDED
```

## Files to Modify

- `src/api/main.py` — Replace hardcoded validation rules with metadata queries (~100–150 lines)
- `migrations/0XX_rel_types_metadata.sql` — Add schema columns + pre-populate existing rel_types
- `tests/api/test_ingest.py` — Add test cases for metadata-driven validation

## Success Criteria

✅ rel_types table expanded with validation metadata columns  
✅ Existing rel_types pre-populated with metadata  
✅ Validation framework queries metadata, not hardcoded rules  
✅ Leaf-only validation applies uniformly (facts + staged_facts)  
✅ Bidirectional validation applies uniformly  
✅ New rel_types self-describe validation via metadata  
✅ All 114+ tests pass, 0 regressions  
✅ No technical debt for future rel_types  

## References

- dBug-report-006/007.md — Identified validation gaps
- dprompt-58/59/62.md — Hardcoded rules (to be replaced)
- CLAUDE.md — Fact classification, ontology self-building, rel_type semantics
- src/api/main.py — `/ingest` validation logic location
- rel_types table — current schema + proposed additions

---

**Status:** Specification complete. Ready for dprompt-65b (formal prompt to deepseek).

**Key philosophy:** Validation rules live in data, not code. Ontology self-describes its constraints. LLM defines as it creates. System enforces consistently.
