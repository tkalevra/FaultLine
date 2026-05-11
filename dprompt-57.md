# dprompt-57: Fix Hierarchy Chain Entity Leakage in Query Results

**Date:** 2026-05-12  
**Author:** Christopher Thompson  
**Status:** Ready for investigation  
**Severity:** P2 (UX — confuses user display, doesn't break data integrity)  
**Related:** dprompt-56b (extraction works), dBug-report-003 (findings)

## Problem Statement

Query results include intermediate type classifications from hierarchy chains as if they're separate owned entities.

**Example:**
- User: "I have a dog named Fraggle, a morkie."
- Stored correctly: `fraggle instance_of morkie`
- Query "tell me about my family" returns: "You have **two dogs**: Morkie and Fraggle"
- Expected: "You have **one dog**: Fraggle (a Morkie)" OR just "Fraggle"

**The issue:** `/query` endpoint walks hierarchy chains and returns intermediate types (Morkie) as separate entities in the final result, instead of filtering them as "type metadata."

## Investigation Goals

1. **Identify where the leak occurs:**
   - Filter output logic (`_filter_relevant_facts()`)? 
   - Backend `/query` baseline facts (`_fetch_user_facts()` UNION)?
   - Hierarchy expansion (`_hierarchy_expand()` marking)?
   - Entity type filtering (Concept types)?
   - Qdrant vector search results?

2. **Understand the flow:**
   - Does `/query` hierarchy expansion intentionally return the full chain?
   - Is Morkie classified as Concept/unknown type that should be filtered?
   - Should intermediate types be marked as "metadata only"?

3. **Determine the fix:**
   - Post-query deduplication (remove intermediate types, keep leaf entities)?
   - Pre-query filtering (exclude type chains from baseline)?
   - Metadata tagging (mark intermediate types so Filter knows not to display)?

## Scope Definition

### MUST Do (Investigation)

1. **Trace the leak** — how does Morkie end up in results when no has_pet fact exists?
   - Check `/query` response JSON — is Morkie in preferred_names, facts, or entity_types?
   - Check Filter logs — does it filter Morkie before display?
   - Check Qdrant — is Morkie in the vector results?

2. **Understand entity_types** — should intermediate types be filtered as Concept?
   - Current: `fraggle` = Animal, `morkie` = unknown
   - Should `morkie` be marked as Concept or Type?
   - Does entity_types filtering in Filter help here?

3. **Review hierarchy expansion** — is `_hierarchy_expand()` returning intermediate types?
   - Should it mark them as "type metadata" vs "entity"?
   - Should `/query` filter these out before returning?

4. **Scope the fix** — what's the smallest, cleanest solution?
   - Option A: Post-query deduplication (Filter removes intermediate types)
   - Option B: Pre-query filtering (baseline/hierarchy exclude type chains)
   - Option C: Entity classification (mark types as Concept, filter them)

### MUST NOT Do

- Change database schema (already correct)
- Modify extraction logic (dprompt-56b is working)
- Break hierarchy expansion (it's used elsewhere)

### MAY Do

- Add entity_types metadata to Filter for type detection
- Create utility function for "is this entity a type classification?"
- Add test case showing correct behavior

## Design Details

### Investigation Queries

**Pre-prod traces:**
1. What does `/query` return for "tell me about my family"?
   - Is Morkie in the response JSON?
   - In which field (preferred_names, facts, entity_types)?

2. What facts does `/query` fetch for the "family" taxonomy context?
   - Does it include Morkie?
   - Is Morkie filtered by Tier 1 entity matching?

3. Does Qdrant include Morkie in results?
   - Vector search for "dog" or "family" — does Morkie rank high?

### Root Cause Options

**Option 1: Hierarchy Expansion Leak**
- `/query` calls `_hierarchy_expand()` to walk instance_of chains
- Returns: fraggle, morkie (both part of the chain)
- Neither filtered before returning to Filter
- **Fix:** Filter out intermediate types after hierarchy expansion

**Option 2: Entity Baseline Leak**
- `_fetch_user_facts()` UNION includes facts mentioning Morkie
- But no has_pet facts exist, so where's the mention coming from?
- Maybe Morkie referenced in a fact about Fraggle?
- **Fix:** Exclude entity_aliases references from baseline

**Option 3: Vector Search Leak**
- Qdrant returns Morkie as similar to "family" queries
- Gets merged into final results without deduplication
- **Fix:** Post-query dedup, keep leaf entities only

**Option 4: Entity Type Classification**
- Morkie should be marked as Concept/Type, not Animal
- Would be filtered by Tier 1 entity matching in Filter
- **Fix:** Update Morkie entity_type → Concept

## Implementation Boundaries

### Investigation Phase (This dprompt)
- Trace where Morkie leaks into results
- Identify root cause (which code path)
- Propose specific fix with evidence
- NO code changes yet

### Fix Phase (Future dprompt — dprompt-57b)
- Implement the fix (likely minimal, isolated change)
- Test locally (no regressions)
- Validate in pre-prod

## Success Criteria

✅ `/query` response traced — identified which field contains Morkie  
✅ Hierarchy expansion traced — does `_hierarchy_expand()` return it?  
✅ Entity baseline traced — does `_fetch_user_facts()` return it?  
✅ Vector search traced — does Qdrant return it?  
✅ Root cause identified — Option 1/2/3/4 or combination  
✅ Fix proposed — specific code path + approach  
✅ Recommendation written to dBug-report-003  

## References

- dBug-report-003.md — detailed symptom and findings
- CLAUDE.md — `/query` path (baseline, graph, hierarchy, vector, entity_types)
- src/api/main.py — `_hierarchy_expand()`, `_fetch_user_facts()`, `/query` endpoint
- openwebui/faultline_tool.py — Filter output logic, `_filter_relevant_facts()`

## Notes for Deepseek

**This is light investigation**, not heavy lifting. The data is clean, extraction works. Something in the query/display logic is including Morkie when it shouldn't.

Start simple:
1. Call `/query` manually with "tell me about my family" → inspect response JSON
2. Grep for where Morkie appears in the response
3. Trace backwards to the code that put it there
4. Propose fix

The answer is probably "post-query dedup removes intermediate types" or "mark Morkie as Concept so it gets filtered earlier," but you need to verify which.
