# dprompt-62: Extend Validation to Staged Facts + Bidirectional Impossibilities

**Date:** 2026-05-13  
**Severity:** P1 (Data integrity — impossible relationships allowed)  
**Status:** Specification ready for implementation  

## Problem

dBug-report-006 found that validation rules don't apply uniformly:

1. **Staged facts bypass extraction constraint:** `owns morkie` created in staged_facts even though dprompt-58 prevents it in facts table
2. **Bidirectional impossibilities allowed:** Des and Cyrus both have child_of AND parent_of relationships to user (semantic impossibility)
3. **Conflict detection incomplete:** dprompt-59 catches some conflicts but misses staged fact patterns

## Solution

Extend validation to apply consistently across all fact entry points and states:

### Part A: Staged Fact Validation (Extract Constraint)

When facts are inserted to staged_facts (Class B unconfirmed):
1. Query: Is the object entity already object of a hierarchy rel (instance_of, subclass_of, member_of, part_of, is_a)?
2. If yes + new fact's rel_type is leaf-only rel (owns, has_pet, works_for, lives_in, lives_at):
3. **DO NOT INSERT** to staged_facts (reject at source, return conflict reason to LLM)

This extends dprompt-59 conflict detection to staged facts BEFORE insertion.

### Part B: Bidirectional Relationship Prevention

When facts are inserted (either facts or staged_facts):
1. Query: Does inverse relationship already exist? (e.g., if inserting child_of, check for parent_of)
2. If both directions exist for same subject-object pair:
   - Keep highest confidence version only
   - Supersede lower confidence (add reason: "bidirectional_conflict: child_of and parent_of cannot both exist")
   - Log audit trail

Examples:
- `user child_of parent` + `user parent_of parent` → keep child_of (correct direction), supersede parent_of
- `des child_of user` + `des parent_of user` → keep child_of, supersede parent_of (confidence determines if neither clear)

### Part C: Consistent Application

Both validations apply at `/ingest` endpoint before fact classification (Class A/B/C assignment).
- Staged facts rejected if they violate hierarchy constraint
- Bidirectional conflicts resolved before commit
- Audit trail logged for all conflicts

## Scope & Integration

**Does NOT change:**
- Fact storage schema (no new columns)
- Query deduplication (dprompt-61)
- Extraction pipeline structure

**Does change:**
- `/ingest` validation logic to apply to staged_facts
- Conflict detection to handle bidirectional impossibilities
- Audit logging for bidirectional supersessions

## Example

**Before (WRONG):**
```
User input: "I have a dog named Fraggle, a morkie"
Extraction: fraggle instance_of morkie, user owns morkie, user has_pet fraggle

Facts table: none (constraint prevented)
Staged facts: owns morkie ✗, has_pet fraggle ✓, owns fraggle ✗
Result: Query shows two dogs (Morkie + Fraggle)
```

**After (CORRECT):**
```
User input: "I have a dog named Fraggle, a morkie"
Extraction: fraggle instance_of morkie, user owns morkie, user has_pet fraggle

Validation at /ingest:
- fraggle instance_of morkie → commit to facts (Class A)
- user owns morkie → REJECT (morkie is hierarchy object, conflicts with constraint)
- user has_pet fraggle → commit to staged_facts (Class B)

Facts table: fraggle instance_of morkie ✓
Staged facts: has_pet fraggle ✓
Result: Query shows one dog (Fraggle, a morkie)
```

## Files to Modify

- `src/api/main.py` — `/ingest` endpoint validation logic
  - Extend `_detect_semantic_conflicts()` to check staged_facts
  - Add `_validate_bidirectional_relationships()` function (~50–80 lines)
  - Call both before Class A/B/C assignment

- `tests/api/test_query.py` — Add test cases
  - Test: staged fact with hierarchy conflict → rejected
  - Test: bidirectional relationship created → lower confidence superseded
  - Test: extraction constraint applied to both facts and staged_facts

## Success Criteria

✅ Staged facts subject to hierarchy conflict detection (no `owns type_entity`)  
✅ Bidirectional impossible relationships prevented (child_of + parent_of cannot coexist)  
✅ Lower-confidence version superseded when conflict detected  
✅ Audit trail logged with conflict reason  
✅ All 114+ tests pass, 0 regressions  
✅ New test cases verify both constraint + bidirectional validation  

## References

- `BUGS/dBug-report-006.md` — Investigation, examples, database state
- `dprompt-58.md/58b.md` — Extraction constraint (incomplete, needs extension to staged_facts)
- `dprompt-59.md/59b.md` — Conflict detection (needs bidirectional rules)
- `CLAUDE.md` — Fact classification, staged_facts semantics, ontology constraints
- `src/api/main.py` — `/ingest` endpoint, `_detect_semantic_conflicts()` location

---

**Ready for dprompt-62b (formal prompt to deepseek).**
