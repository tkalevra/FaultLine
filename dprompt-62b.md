# dprompt-62b: DEEPSEEK_INSTRUCTION_TEMPLATE — Staged Fact Validation + Bidirectional Rules

## Task

Extend validation to staged_facts and prevent bidirectional impossible relationships (child_of + parent_of coexisting).

## Context

dBug-report-006 found that validation rules don't apply uniformly:
- dprompt-58 extraction constraint prevents `owns morkie` in facts table, but NOT in staged_facts
- dprompt-59 conflict detection incomplete for staged patterns
- Bidirectional impossibilities allowed (Des, Cyrus both child_of AND parent_of user)

**Why:** Staged facts are unconfirmed Class B facts waiting for 3 confirmations. They're still part of the graph and should follow same semantic rules as committed facts. Bidirectional relationships (child_of + parent_of for same pair) are semantic impossibilities that contradict each other.

**Integration:** Extends `/ingest` validation BEFORE fact classification (Class A/B/C). Works alongside dprompt-58 (extraction constraint) and dprompt-59 (conflict detection).

**Reference:** Read `dprompt-62.md` (spec), `BUGS/dBug-report-006.md` (investigation), `CLAUDE.md` (staged_facts semantics).

## Constraints

### MUST:
- Extend `_detect_semantic_conflicts()` to check staged_facts when validating new facts
- Prevent `owns/has_pet/works_for/lives_in/lives_at` for entities that are objects of hierarchy rels (instance_of/subclass_of/member_of/part_of/is_a) — applies to both facts and staged_facts
- Detect bidirectional impossible relationships (child_of + parent_of for same subject-object pair)
- When bidirectional conflict found: supersede lower-confidence version, log reason "bidirectional_conflict"
- Apply validation BEFORE Class A/B/C assignment in `/ingest` pipeline
- Pass all existing tests (114+ in suite, 0 new failures)

### DO NOT:
- Modify fact storage schema
- Change extraction pipeline (dprompt-58 stays in Filter)
- Break staged_facts promotion logic (confirmed_count >= 3 still works)
- Expose conflict logic to Filter (backend-only)

### MAY:
- Add new helper function `_validate_bidirectional_relationships()`
- Include performance notes if validation adds latency
- Log conflict details for audit trail

## Sequence

### 1. Read & Understand (No coding)

- Read `dprompt-62.md` (specification, examples, scope)
- Read `BUGS/dBug-report-006.md` (investigation, staged facts findings, bidirectional examples)
- Read `CLAUDE.md` section on staged_facts, fact classification, ontology constraints
- Read `src/api/main.py`:
  - Locate `/ingest` endpoint
  - Find `_detect_semantic_conflicts()` function (added in dprompt-59)
  - Understand where Class A/B/C assignment happens
  - Confirm validation runs before assignment

Confirm: Where should bidirectional validation fit in the sequence?

### 2. Extend Conflict Detection (Staged Facts)

**File:** `src/api/main.py`, `_detect_semantic_conflicts()` function

**Current behavior (dprompt-59):**
```python
def _detect_semantic_conflicts(new_fact):
    # Check if object is hierarchy target
    # If yes + rel_type is leaf-only → mark as superseded
```

**Task:** Add check for staged_facts
```python
# Also query staged_facts table (not just facts)
# If object found in staged_facts with hierarchy rel → same conflict logic
```

**Location:** Inside `_detect_semantic_conflicts()`, after querying facts table, add parallel query for staged_facts.

### 3. Add Bidirectional Validation

**New function:** `_validate_bidirectional_relationships(subject_id, rel_type, object_id, confidence)`

**Logic:**
```
1. Determine inverse rel_type:
   - child_of ↔ parent_of
   - spouse ↔ spouse (symmetric, no inverse)
   - has_pet, owns → no inverse (leaf-only)

2. Query facts + staged_facts for inverse relationship:
   - Look for (subject_id, inverse_rel_type, object_id)
   
3. If found:
   - Compare confidence: keep higher, supersede lower
   - Update facts.superseded_at or staged_facts (if moving from staged to facts)
   - Log audit reason: "bidirectional_conflict: {rel_type} and {inverse_rel_type} cannot coexist"
```

**Integration:** Call BEFORE Class A/B/C assignment in `/ingest` pipeline.

### 4. Call Validation in /ingest

**Location:** `/ingest` endpoint, before the fact classification block (before Class A/B/C assignment)

**Sequence:**
```python
# 1. Extract edges via LLM
# 2. GLiNER2 fallback/override
# 3. WGMValidationGate (ontology + existing constraints)
# 4. _detect_semantic_conflicts() [EXTEND TO STAGED]
# 5. _validate_bidirectional_relationships() [NEW]
# 6. Fact classification (Class A/B/C assignment)
```

### 5. Test Locally

**Run test suite:**
```bash
pytest tests/api/test_query.py -v
```

Expected: 114+ tests pass, 0 regressions.

**Spot-check staged fact validation:**

```python
# Test: Extract "I have a dog named Fraggle, a morkie"
# Expected: has_pet fraggle (staged), NOT owns morkie (conflict detected)

# Test: Extract conflicting parent_of relationships
# Expected: if child_of exists, parent_of superseded with lower confidence
```

### 6. STOP & Report

Update `scratch.md` with template below. Do NOT proceed to deployment.

## Deliverable

**Modified file:** `src/api/main.py`

- Extended `_detect_semantic_conflicts()` to check staged_facts (~20–40 lines)
- Added `_validate_bidirectional_relationships()` function (~50–80 lines)
- Both called in `/ingest` before Class A/B/C assignment
- Audit logging for all conflicts

**New test cases:** `tests/api/test_query.py`

- Test: staged fact with hierarchy conflict → rejected
- Test: bidirectional relationship created → lower confidence superseded
- Test: extraction constraint applies to both facts and staged_facts

## Files to Modify

```
src/api/main.py
├─ _detect_semantic_conflicts() [extend to staged_facts]
├─ _validate_bidirectional_relationships() [new function]
└─ /ingest endpoint [call both before Class A/B/C]

tests/api/test_query.py
└─ Add 3 test cases for staged fact + bidirectional validation
```

## Success Criteria

✅ Staged facts validation: `owns type_entity` rejected  
✅ Bidirectional prevention: child_of + parent_of cannot coexist  
✅ Lower-confidence version superseded when conflict found  
✅ Audit trail logged with reason  
✅ Tests: 114+ pass, 0 regressions, 3 new cases passing  
✅ No schema changes, no extraction pipeline changes  

## Upon Completion

**⚠️ MANDATORY: Update scratch.md (FaultLine-dev) with this template, then STOP:**

```markdown
## ✓ DONE: dprompt-62 (Staged Fact Validation + Bidirectional Rules) — [DATE]

**Task:** Extend validation to staged_facts and prevent bidirectional impossible relationships.

**Implementation (src/api/main.py):**
- Extended `_detect_semantic_conflicts()` to check staged_facts
  - Lines: [START] → [END]
  - Added parallel query for staged_facts hierarchy conflicts
  
- Added `_validate_bidirectional_relationships()` function
  - Lines: [START] → [END]
  - Prevents child_of + parent_of coexistence
  - Supersedes lower-confidence version on conflict
  
- /ingest endpoint calls both before Class A/B/C assignment

**Tests (tests/api/test_query.py):**
- Test: staged fact with hierarchy conflict → rejected ✓
- Test: bidirectional relationship → lower confidence superseded ✓
- Test: extraction constraint applies to staged_facts ✓
- All 114+ existing tests pass ✓

**Validation:**
- Staged facts now subject to hierarchy conflict detection
- Bidirectional impossibilities prevented
- Audit trail logged for all conflicts
- Zero regressions

**Example result:**
- Input: "I have a dog Fraggle, a morkie"
- Before: has_pet fraggle + owns morkie (conflicting, staged_facts)
- After: has_pet fraggle (only) + morkie rejected (conflict)

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, wait for user direction.

## Critical Rules (Non-Negotiable)

**Validation timing:** Runs BEFORE Class A/B/C assignment in `/ingest`. Catches conflicts at entry point.

**Staged facts handling:** Must check staged_facts table as well as facts table. Unconfirmed facts still part of graph.

**Bidirectional detection:** If both inverse relationships exist, keep higher confidence, supersede lower.

**Test discipline:** 114+ existing tests must pass. New tests verify both constraint + bidirectional logic.

**STOP clause mandatory:** Every implementation ends with STOP. User must rebuild, validate, then decide next.

---

**Template version:** 1.0 (follows DEEPSEEK_INSTRUCTION_TEMPLATE)  
**Status:** Ready for execution by deepseek
