# dprompt-61: Query Deduplication via is_preferred Flag

**Date:** 2026-05-12  
**Severity:** P1 (UX — query results show duplicate facts due to alias redundancy)  
**Status:** Specification ready for implementation  

## Problem

Query response for "Tell me about my family" includes duplicate facts when an entity has multiple aliases (e.g., christopher/chris):

```
Current (WRONG):
- christopher spouse mars (conf 0.5)
- chris spouse mars (conf 0.5) — DUPLICATE via alias
- Result: "Your spouse is Mars and your wife..." (appears as two separate facts)

Expected (RIGHT):
- user spouse mars (deduped, enriched with alias context)
- Result: "Your spouse is Mars (also known as Marla, Marissa)"
```

Root cause: `/query` returns facts for all entity aliases without deduplication. When the same triple exists for multiple aliases (christopher/chris), results contain duplicate facts.

## Solution

Modify `/query` response building to:

1. **Filter facts to use only `is_preferred=true` aliases**
   - When collecting facts, prefer entities via their preferred alias
   - Skip alternate aliases (is_preferred=false) to avoid duplicate triples

2. **Deduplicate by `(subject_id, rel_type, object_id)`**
   - Keep one fact per unique relationship
   - If multiple aliases map to same subject/object, keep highest-confidence version

3. **Attach `_aliases` metadata**
   - Include all aliases for both subject and object with is_preferred flag
   - Display names only (never expose UUIDs)
   - Preserve name relationships in metadata (e.g., mars/marla in _aliases, not in facts)

4. **Return enriched facts with single structure per relationship**
   - No duplicate fact objects
   - Alias context embedded in metadata
   - Natural language can use _aliases for fallbacks: "Mars (also known as Marla)"

## Scope & Constraints

**This solution must integrate with:**
- **Graph traversal** (`_graph_traverse()`) — finds connected entities via relationships
- **Hierarchy expansion** (`_hierarchy_expand()`) — walks composition chains (instance_of, subclass_of, part_of, member_of, is_a)
- **Baseline facts** (`_fetch_user_facts()`) — identity-anchored scalar and relationship facts

**Deduplication happens AFTER collection**, not during. All facts are still retrieved (including hierarchies), deduplicated by entity_id, and enriched with alias context.

**Does NOT affect:**
- Fact storage (PostgreSQL facts table unchanged)
- Graph structure (relationships preserved)
- Hierarchy chains (instance_of/subclass_of chains intact)
- Qdrant indexing (derived from deduplicated results)

## Example

**Database state:**
```
Facts table:
- (christopher, spouse, mars, conf=0.5)
- (chris, spouse, mars, conf=0.5)  [chris is non-preferred alias for user]
- (mars, same_as, marla, conf=0.4)

Entity aliases:
- christopher (is_preferred=false)
- chris (is_preferred=false)
- marla (is_preferred=false)
- mars (is_preferred=true)
```

**Current /query response (WRONG):**
```json
{
  "preferred_names": ["user", "christopher", "chris"],
  "facts": [
    {"subject": "christopher", "rel_type": "spouse", "object": "mars", "confidence": 0.5},
    {"subject": "chris", "rel_type": "spouse", "object": "mars", "confidence": 0.5}
  ]
}
```

**Expected /query response (RIGHT):**
```json
{
  "preferred_names": ["user"],
  "facts": [
    {
      "subject": "user",
      "rel_type": "spouse",
      "object": "mars",
      "confidence": 0.5,
      "_aliases": {
        "subject": [{"name": "christopher", "is_preferred": false}, {"name": "chris", "is_preferred": false}],
        "object": [{"name": "marla", "is_preferred": false}, {"name": "mars", "is_preferred": true}]
      }
    }
  ]
}
```

**Natural language result:**
```
Your spouse is Mars (also known as Marla).
```

## Files to Modify

- `src/api/main.py` — `/query` endpoint, fact collection and deduplication logic (~50–100 lines)
  - After `_fetch_user_facts()` UNION (baseline facts)
  - After `_graph_traverse()` and `_hierarchy_expand()` merging
  - Before final fact list is returned to Filter
  - Add deduplication: filter by is_preferred, deduplicate by (subject_id, rel_type, object_id), build _aliases metadata

- `tests/api/test_query.py` — Add test cases for alias deduplication
  - Test: multiple aliases for same entity → single deduplicated fact
  - Test: hierarchy chains with aliases → chains intact, facts deduplicated
  - Test: _aliases metadata populated correctly

## Success Criteria

✅ Query returns one fact per (subject_id, rel_type, object_id) triple  
✅ Facts use `is_preferred=true` aliases as subject/object display names  
✅ `_aliases` metadata includes all aliases with preference flag  
✅ Hierarchy chains (instance_of, subclass_of, etc.) preserved in full  
✅ Graph traversal still works (connectivity unaffected)  
✅ Natural language output: "Your spouse is Mars (also known as Marla)" — no duplicates  
✅ All existing tests pass (114+ in suite, 0 new failures)  
✅ No UUIDs exposed in responses (display names only)  

## References

- `BUGS/dBug-report-005.md` — Full investigation, examples, test cases
- `CLAUDE.md` — `/query` path, entity alias management, is_preferred flag semantics
- `src/api/main.py` — `/query` endpoint, fact collection order (baseline → graph → hierarchy → Qdrant → merge)
- `openwebui/faultline_tool.py` — Filter ingests fact list, builds natural language from facts + metadata

---

**Ready for dprompt-61b (formal prompt to deepseek).**
