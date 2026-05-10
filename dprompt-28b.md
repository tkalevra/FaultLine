# dprompt-28b — Hierarchy Traversal: Composition + Classification [PROMPT]

## #deepseek NEXT: dprompt-28b — Hierarchy Expansion (after dprompt-27b) — 2026-05-10

### Task:
Implement `_hierarchy_expand()` function and integrate it into `/query` endpoint to enrich entities with their hierarchical classification and composition context.

### Context:

dprompt-27b implemented graph traversal (finds connected entities). Now we add hierarchy traversal (enriches those entities with "what they are" and "what they belong to"). Together they enable rich entity context:

- Graph: "Mars → spouse → connected to user" + "Mars → has_pet → Fraggle"
- Hierarchy: "Mars instance_of Person subclass_of Family" + "Fraggle instance_of Morkie subclass_of Dog subclass_of Animal"
- Result: Query returns both entities + full taxonomic context

dprompt-28.md contains the technical spec with SQL CTE examples and upward/downward traversal patterns.

### Constraints:

- Wrong: Fetching all facts then filtering by hierarchy (inefficient)
- Right: Query hierarchies explicitly, return enriched entity set, fetch facts for enriched set only
- MUST: Implement upward traversal (entity → classification chain) first
- MUST: Use SQL CTE (WITH RECURSIVE) to prevent infinite loops on cycles
- MUST: Respect max_depth to limit chain length
- MAY: Implement downward traversal (class → members) if time permits; upward is critical

### Sequence (DO NOT skip or reorder):

1. Read dprompt-28.md spec (full document) to understand upward vs downward chains
2. Review the SQL CTE examples (dprompt-28.md, "Database Queries" section) for correct recursion pattern
3. Implement `_hierarchy_expand(entity_id, direction="up", max_depth=3, user_id=None)` function
   - direction="up": entity → instance_of → subclass_of chain (to classification root)
   - direction="down": class → instance_of ← subjects (to members) — optional, lower priority
   - Returns set of entity UUIDs in the chain (including the starting entity)
4. Integrate into `/query` after dprompt-27b's graph traversal:
   - For each entity returned by graph traversal, call `_hierarchy_expand(entity, direction="up", max_depth=3)`
   - Accumulate all entities (graph results + hierarchy chains) into `enriched_entities` set
   - Fetch facts for enriched_entities set (reuse `_fetch_user_facts()` with filtering)
5. Test: "where do mars and fraggle live?" returns hierarchy context (mars → person → family, fraggle → morkie → dog → animal)

### Deliverable:

- `_hierarchy_expand(entity_id, direction="up", max_depth=3, user_id)` function
  - Uses SQL CTE with cycle protection
  - Returns set of entity UUIDs in hierarchy chain
- `/query` integration after graph traversal:
  - For each graph result, expand hierarchy
  - Accumulate into enriched_entities set
  - Fetch facts for enriched set
- Upward traversal (entity → classification) fully working
- Downward traversal (class → members) optional; mention if skipped

### Files to Modify:

- `src/api/main.py` — Add `_hierarchy_expand()` function; integrate into `/query` loop after graph traversal call

### Success Criteria:

- Test: `_hierarchy_expand(fraggle_uuid, direction="up", max_depth=3)` returns {fraggle, morkie, dog, animal}
- Test: Integration with graph traversal: "where do mars and fraggle live?" returns both entities + full hierarchy context
- Test: CTE handles cycles gracefully (no infinite loops)
- Code parses cleanly: `python -m py_compile src/api/main.py`
- Existing tests still pass; no regressions
- Performance: graph + hierarchy expand complete in <200ms for typical queries

### Upon Completion:

**Update scratch.md with this entry:**
```
## ✓ DONE: dprompt-28b (Hierarchy Expansion) — 2026-05-10

- Implemented _hierarchy_expand(entity_id, direction="up", max_depth=3, user_id)
- SQL CTE prevents infinite recursion on cycles
- Integrated into /query after graph traversal
- Upward traversal (entity → classification) fully working ✓
- Downward traversal (class → members): [DONE / SKIPPED] (note if skipped)
- Test scenario "where do mars and fraggle live?" verified with hierarchy context ✓
- Query flow: graph traverse → hierarchy expand → fetch facts → merge/score → inject
- System ready for production query expansion

**Next:** [describe any pending work or declare MVP complete]
```

### Notes:

- Upward traversal is critical. Downward can be phased in later if time is tight.
- If you skip downward traversal, note it in the completion update so we know for next iteration.
- The CTE pattern in dprompt-28.md is SQL-agnostic; adapt if your PostgreSQL version needs tweaks.
- No changes to baseline facts, Qdrant, or score/merge logic from dprompt-27b.
