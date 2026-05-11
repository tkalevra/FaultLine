# dprompt-59: Conflict Detection & Auto-Superseding via Graph Semantics

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Ready for specification  
**Severity:** P1 (data quality — keeps graph semantically valid)  
**Related:** dprompt-58 (extraction constraint), dBug-report-003/004 (conflict findings)

## Problem Statement

The system has extraction constraints (dprompt-58) and reactive retraction signals, but lacks **proactive conflict detection**. When conflicting facts enter the ingest pipeline, they violate graph semantics without being caught.

**Example:** 
- `fraggle instance_of morkie` exists (morkie is a TYPE)
- User input creates `user owns morkie` (contradicts type semantics)
- Current behavior: Both facts stored, graph is semantically inconsistent
- Desired: Detect contradiction, auto-supersede the conflicting `owns` fact

**Why it matters:** The graph KNOWS the semantics. The ontology defines what `instance_of` means. The system should use that knowledge to keep data clean, not wait for explicit user correction.

## Semantic Principles

**The graph IS the source of truth:**
1. **Identity/classification relationships** (instance_of, subclass_of, is_a) define WHAT things are
2. **Composition/membership relationships** (part_of, member_of) define how things structure together
3. **Independent relationships** (owns, has_pet, lives_in, works_for) define properties of LEAF entities, not types/containers

**Conflict patterns to detect:**

| Pattern | Detection | Resolution |
|---------|-----------|-----------|
| Type relationship → Ownership of type | `A instance_of B` + `subject owns B` | Supersede `owns B`; keep type classification |
| Component → Ownership of component | `A part_of B` + `subject owns B` | Supersede `owns B`; keep component relationship |
| Hierarchy contradiction | `A subclass_of B` + `B subclass_of A` | Flag/investigate; likely data error |
| Role/type dual-extraction | `A instance_of engineer` + `A works_for engineer` | Supersede `works_for engineer`; keep role classification |
| Container confusion | `A part_of Ontario` + `subject lives_in Ontario` | Keep `lives_in A` (leaf); supersede `lives_in Ontario` if both exist |

## Solution Overview

Add **conflict detection to the ingest pipeline** that:
1. **Before WGM gate:** Check if new fact contradicts existing graph structure
2. **Uses ontology:** Query rel_type semantics (hierarchy, composition, properties)
3. **Identifies conflicts:** Does this violate known semantic patterns?
4. **Auto-resolves:** Mark conflicting facts as superseded, keep semantically correct ones
5. **Logs decisions:** Record why conflicts were resolved (for audit trail)

## Scope Definition

### MUST Do (Implementation)

1. **Create conflict detection logic** (in `/ingest` before WGM gate)
   - Query existing graph for the subject/object entities
   - Check ontology constraints for the rel_type
   - Identify contradiction patterns (type vs ownership, container vs independent, etc.)
   - Decision tree: supersede conflicting fact, keep valid fact, or flag for review

2. **Implement for priority patterns:**
   - Type/instance contradictions (instance_of/subclass_of + owns/has_pet)
   - Component contradictions (part_of + owns)
   - Role contradictions (instance_of role + works_for role)
   - Container/leaf contradictions (location hierarchy + lives_in)

3. **Preserve audit trail:**
   - Log conflict detection decisions (which fact was superseded, why)
   - Include in fact metadata: `contradicted_by`, reason string
   - Allow future queries to understand resolution history

4. **Validate with existing graph:**
   - Query `_hierarchy_expand()` to check if object is a type/class
   - Check `_REL_TYPE_HIERARCHY` to identify type-defining rels
   - Use `_graph_traverse()` to find conflicting paths

5. **Test locally:**
   - No new regressions (existing tests still pass)
   - Manual testing: "I have a dog named Fraggle, a morkie" should NOT create `user owns morkie`
   - Verify: existing bad facts can be superseded via correction flow

### MUST NOT Do

- Change WGM gate validation (keep it strict)
- Modify `/retract` endpoint (keep retraction flow independent)
- Break extraction prompt (dprompt-58 is authoritative)
- Auto-delete facts (only supersede with reasons)

### MAY Do

- Add admin endpoint to review conflict resolution history
- Create dashboard showing detected conflicts + resolutions
- Add configurable sensitivity (e.g., "strict" vs "lenient" conflict detection)

## Design Details

### Conflict Detection Algorithm

**Input:** New fact `(subject, rel_type, object)`  
**Output:** Decision {keep, supersede_existing, supersede_new, flag_review}

```
1. Query: does `object` appear as object of any `instance_of`, `subclass_of`, `is_a`, `member_of`, `part_of`?
   → If yes, `object` is a TYPE/CATEGORY/COMPONENT, not a leaf entity
   
2. If object is TYPE and rel_type in [owns, has_pet, works_for, lives_in, ...]:
   → CONFLICT: trying to own/relate to a type, not an instance
   → DECISION: supersede new `owns/has_pet/works_for/lives_in` fact
   → KEEP: the hierarchy relationship (it's semantically correct)

3. If subject is already object of hierarchy rel, and new rel is independent:
   → CONFLICT: subject is a type, shouldn't have independent properties
   → DECISION: depends on confidence and user context
   
4. If contradictory hierarchy (A subclass_of B, B subclass_of A):
   → CONFLICT: cyclic hierarchy
   → DECISION: flag for review (likely user error or data corruption)
```

### Example Flows

**Scenario 1: Type/Ownership conflict**
```
User: "I have a dog named Fraggle, a morkie mix"

New fact 1: fraggle instance_of morkie    [conf 1.0, Class A]
New fact 2: user owns morkie               [conf 0.8, Class B]

Detection:
  - Check: Is morkie object of hierarchy rel? 
  - Query: SELECT * FROM facts WHERE object_id=morkie_uuid AND rel_type IN ('instance_of', 'subclass_of', ...)
  - Result: Yes (fraggle instance_of morkie exists)
  - morkie is a TYPE, not separate entity
  
Decision:
  - KEEP: fraggle instance_of morkie (correct)
  - SUPERSEDE: user owns morkie (conflicting)
  - Log: "superseded (type_ownership_conflict): user owns morkie contradicts fraggle instance_of morkie"
  
Result:
  - fraggle instance_of morkie → facts table
  - user owns morkie → superseded (not stored, or stored with superseded_at=now())
```

**Scenario 2: Role/type conflict**
```
User: "Alice is an engineer in Finance"

New fact 1: alice instance_of engineer     [conf 1.0, Class A]
New fact 2: alice works_for engineer       [conf 0.7, Class B]  ← conflicting

Detection:
  - alice is object of instance_of engineer
  - engineer is a ROLE/TYPE
  - works_for engineer doesn't make sense (she IS an engineer, doesn't work FOR engineer)

Decision:
  - KEEP: alice instance_of engineer (correct)
  - SUPERSEDE: alice works_for engineer (conflicting)
  - KEEP: alice works_for finance (if extracted, this is correct)
  
Result:
  - Only valid facts stored; type/role confusion resolved
```

## Implementation Boundaries

### Ingest Pipeline Changes (src/api/main.py)

New function: `detect_conflicts(fact, db) -> {decision, reason, supersede_ids}`

Call sites:
- Before `/ingest` WGM gate: check new fact for conflicts with existing graph
- If decision="supersede_existing": mark those facts as `superseded_at=now()`
- If decision="supersede_new": don't store the new fact
- If decision="flag_review": store fact but mark as `needs_review=true`
- Log decision to audit trail

### No changes to:
- Filter extraction logic (dprompt-58 handles that)
- `/retract` endpoint (retraction flow stays independent)
- WGM gate constraints (keep those strict)
- Database schema (use existing `superseded_at`, `contradicted_by` columns)

## Success Criteria

✅ Conflict detection algorithm implemented  
✅ Priority patterns handled (type/ownership, role/type, component/ownership)  
✅ Audit trail preserved (decisions logged with reasons)  
✅ Test coverage: manual scenarios pass (Fraggle/morkie, Alice/engineer, etc.)  
✅ No regressions: existing tests pass (114+)  
✅ Graph remains semantically valid (verified via queries)  

## References

- dprompt-58.md — extraction constraint (prevents bad extractions)
- dBug-report-003.md — conflict discovery
- dBug-report-004.md — cleanup scope
- CLAUDE.md — graph semantics, ontology principles
- src/api/main.py — `_hierarchy_expand()`, `_graph_traverse()`, `/ingest` endpoint

## Notes for Deepseek

**The system knows the truth:** The graph, ontology, and hierarchy relationships encode semantic meaning. When a new fact violates that meaning, the system should recognize it and clean it up automatically.

**Examples of semantic validity:**
- If X is a TYPE (object of instance_of), X is not a separate entity with ownership relationships
- If X is a COMPONENT (object of part_of), X is not independently ownedable
- If X is a ROLE (object of instance_of + role context), X is not a separate entity with works_for relationships
- If X is a LOCATION CONTAINER (object of part_of), entities live IN contained locations, not in the container itself

Build conflict detection that respects these patterns. Use the graph to validate new facts against existing semantics.

This keeps the graph clean without requiring explicit user corrections.
