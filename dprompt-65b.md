# dprompt-65b: DEEPSEEK_INSTRUCTION_TEMPLATE — Metadata-Driven Validation Framework

## Task

Replace hardcoded validation rules with metadata-driven framework. Store rel_type properties (symmetric, inverse, leaf_only, hierarchy) in database. Validation queries metadata at runtime, enabling dynamic ontology without technical debt.

## Context

dBug-report-006/007 revealed validation gaps (bidirectional impossibilities, UUID exposure). Current approach hardcodes rules for each rel_type. As LLM creates new rel_types dynamically, validation rules become outdated, requiring new dprompt for each edge case (dprompt-63, dprompt-64, etc.). This accumulates technical debt.

**Why:** Validation rules should live in data (rel_types table), not code. When LLM creates a new rel_type, it defines its validation properties. System applies rules uniformly without code changes.

**Integration:** Replaces hardcoded validation in dprompt-58/59/62. Applies to all rel_types (current and future). Works alongside dynamic ontology self-building.

**Reference:** Read `dprompt-65.md` (specification), `CLAUDE.md` (rel_type semantics), `dBug-report-006/007.md` (validation gaps).

## Constraints

### MUST:
- Add validation metadata columns to rel_types table (is_symmetric, inverse_rel_type, is_leaf_only, is_hierarchy_rel, allows_leaf_rels)
- Pre-populate metadata for all existing rel_types (parent_of, spouse, instance_of, etc.)
- Replace hardcoded validation rules in `/ingest` with metadata queries
- Apply validation uniformly to facts and staged_facts
- Support LLM providing metadata when creating novel rel_types
- Pass all 114+ tests, 0 regressions

### DO NOT:
- Modify fact storage schema (ontology metadata in rel_types only)
- Break extraction pipeline (LLM still extracts rel_types, now with optional metadata)
- Change query/retrieval logic (data layer only)
- Hardcode new rel_type rules (all validation via metadata)

### MAY:
- Add new helper function to query rel_type metadata
- Include performance notes if metadata queries add latency
- Add logging for metadata lookup during validation

## Sequence

### 1. Read & Understand (No coding)

- Read `dprompt-65.md` (specification, examples, philosophy)
- Read `dBug-report-006/007.md` (what validation failures looked like)
- Read `CLAUDE.md` section on rel_types, self-building ontology, fact classification
- Read `src/api/main.py`:
  - Current `/ingest` validation logic (hardcoded rules)
  - Current validation checks in `_detect_semantic_conflicts()`, `_validate_bidirectional_relationships()`
  - Confirm where validation runs (before Class A/B/C assignment)

Confirm: Where should metadata queries fit in the validation flow?

### 2. Create Migration (rel_types Schema)

**File:** `migrations/0XX_rel_types_metadata.sql` (new file, increment migration number)

**Schema additions:**
```sql
ALTER TABLE rel_types ADD COLUMN (
  is_symmetric BOOLEAN DEFAULT FALSE,
  inverse_rel_type VARCHAR(100),
  is_leaf_only BOOLEAN DEFAULT FALSE,
  is_hierarchy_rel BOOLEAN DEFAULT FALSE,
  allows_leaf_rels TEXT[]
);
```

**Pre-populate existing rel_types with metadata:**

```sql
-- Existing rel_types (hardcoded, from CLAUDE.md)
UPDATE rel_types SET is_symmetric=true WHERE rel_type IN ('spouse', 'sibling_of', 'knows', 'friend_of', 'met', 'same_as');
UPDATE rel_types SET inverse_rel_type='child_of' WHERE rel_type='parent_of';
UPDATE rel_types SET inverse_rel_type='parent_of' WHERE rel_type='child_of';
UPDATE rel_types SET is_leaf_only=true WHERE rel_type IN ('owns', 'has_pet', 'works_for', 'lives_in', 'lives_at', 'educated_at');
UPDATE rel_types SET is_hierarchy_rel=true WHERE rel_type IN ('instance_of', 'subclass_of', 'member_of', 'part_of', 'is_a');
UPDATE rel_types SET allows_leaf_rels=ARRAY['has_pet', 'owns', 'works_for', 'lives_in', 'lives_at'] WHERE rel_type IN ('instance_of', 'subclass_of', 'member_of');
```

### 3. Refactor Validation Logic

**File:** `src/api/main.py`, `/ingest` endpoint

**Location:** Replace/refactor `_detect_semantic_conflicts()` and `_validate_bidirectional_relationships()` to use metadata queries.

**New helper function:**
```python
def _get_rel_type_metadata(rel_type):
  # Query rel_types table for validation properties
  # Cache result to avoid repeated queries
  # Return metadata dict
```

**Replace hardcoded checks:**
```python
# OLD (hardcoded):
if rel_type in ['owns', 'has_pet', 'works_for']:  # leaf-only rels
  if is_hierarchy_object(object_id):
    return CONFLICT()

# NEW (metadata-driven):
rel_metadata = _get_rel_type_metadata(rel_type)
if rel_metadata.is_leaf_only and is_hierarchy_object(object_id):
  return CONFLICT()
```

**Apply to both:**
- Facts table ingest (Class A/B/C assignment)
- Staged_facts validation

### 4. Test Locally

**Run test suite:**
```bash
pytest tests/api/test_ingest.py -v
```

Expected: 114+ tests pass, 0 regressions.

**Spot-check scenarios:**

```python
# Test 1: Leaf-only validation via metadata
# Input: "alice owns engineer_role" (engineer_role is type/hierarchy object)
# Expected: CONFLICT (owns is_leaf_only, object is hierarchy target)

# Test 2: Symmetric validation
# Input: Extract spouse twice, once forward once reverse
# Expected: BOTH stored (is_symmetric=true)

# Test 3: Bidirectional prevention
# Input: Extract parent_of then child_of for same pair
# Expected: One direction only, lower confidence superseded

# Test 4: Novel rel_type with metadata
# Input: LLM creates "supervises" with metadata { is_leaf_only: true, inverse: "supervised_by" }
# Expected: "alice supervises engineer_role" rejected (leaf_only), validation works without code change
```

### 5. STOP & Report

Update `scratch.md` with template below. Do NOT proceed to deployment.

## Deliverable

**Modified files:**
- `src/api/main.py` — Replace hardcoded validation with metadata queries (~100–150 lines)
- `migrations/0XX_rel_types_metadata.sql` — Schema expansion + pre-population

**New test cases:** `tests/api/test_ingest.py`
- Test: Leaf-only validation via metadata
- Test: Symmetric rel_type allows bidirectional
- Test: Bidirectional prevention via metadata
- Test: Novel rel_type self-describes validation

## Files to Modify

```
src/api/main.py
├─ /ingest endpoint validation
│  ├─ _get_rel_type_metadata() [new helper]
│  ├─ _detect_semantic_conflicts() [refactor to use metadata]
│  └─ _validate_bidirectional_relationships() [refactor to use metadata]

migrations/0XX_rel_types_metadata.sql [new]
├─ ALTER TABLE rel_types (add metadata columns)
└─ UPDATE rel_types (pre-populate for existing rel_types)

tests/api/test_ingest.py
└─ Add 4 test cases for metadata-driven validation
```

## Success Criteria

✅ rel_types table has metadata columns (is_symmetric, inverse_rel_type, is_leaf_only, is_hierarchy_rel, allows_leaf_rels)  
✅ Existing rel_types pre-populated with metadata  
✅ Validation framework queries metadata instead of hardcoded rules  
✅ Leaf-only validation works via metadata (facts + staged_facts)  
✅ Bidirectional validation works via metadata  
✅ Symmetric rel_types allow bidirectional storage  
✅ New rel_types self-describe validation via metadata  
✅ Tests: 114+ pass, 0 regressions, 4 new test cases passing  
✅ No hardcoded validation rules in code (all queries metadata)  

## Upon Completion

**⚠️ MANDATORY: Update scratch.md (FaultLine-dev) with this template, then STOP:**

```markdown
## ✓ DONE: dprompt-65 (Metadata-Driven Validation Framework) — [DATE]

**Task:** Replace hardcoded validation rules with metadata-driven framework enabling dynamic ontology.

**Implementation (src/api/main.py):**
- Added `_get_rel_type_metadata()` helper function
  - Caches metadata queries to avoid repeated lookups
  - Returns validation properties for any rel_type
  
- Refactored `_detect_semantic_conflicts()` to query metadata
  - Lines: [START] → [END]
  - Leaf-only validation now metadata-driven
  - Applies to facts + staged_facts
  
- Refactored `_validate_bidirectional_relationships()` to query metadata
  - Lines: [START] → [END]
  - Inverse pair detection via metadata
  - Symmetric rel_types allow bidirectional

**Migration (migrations/0XX_rel_types_metadata.sql):**
- Schema: Added is_symmetric, inverse_rel_type, is_leaf_only, is_hierarchy_rel, allows_leaf_rels
- Pre-populated: All existing rel_types with metadata (parent_of, spouse, instance_of, etc.)

**Tests (tests/api/test_ingest.py):**
- Test: Leaf-only validation via metadata ✓
- Test: Symmetric rel_type allows bidirectional ✓
- Test: Bidirectional prevention via metadata ✓
- Test: Novel rel_type self-describes validation ✓
- All 114+ existing tests pass ✓

**Validation:**
- Hardcoded rules eliminated (all queries metadata)
- Framework scales with dynamic ontology
- New rel_types self-describe without code changes
- No technical debt

**Example result:**
- Input: LLM creates "supervises" with metadata { is_leaf_only: true, inverse: "supervised_by" }
- "alice supervises engineer" auto-rejected (leaf_only)
- Validation works WITHOUT new dprompt
- System scales

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, wait for user direction.

## Critical Rules (Non-Negotiable)

**Metadata-first philosophy:** All validation properties stored in data, not code. No hardcoded rel_type rules.

**Schema isolation:** Metadata in rel_types table only. No changes to facts/staged_facts schema.

**Migration safety:** Pre-populate existing rel_types before any validation code change. Ensure migration runs before `/ingest` uses metadata.

**Test discipline:** 114+ existing tests must pass. New tests verify metadata queries work. Zero regressions.

**STOP clause mandatory:** Every implementation ends with STOP. User must rebuild pre-prod, validate, then decide next.

---

**Template version:** 1.0 (follows DEEPSEEK_INSTRUCTION_TEMPLATE)  
**Philosophy:** Validation rules live in data. Ontology self-describes constraints. LLM defines as it creates.  
**Status:** Ready for execution by deepseek
