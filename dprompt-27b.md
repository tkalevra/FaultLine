# dprompt-27b — Query Redesign: Graph Traversal + Hierarchy Expansion [PROMPT]

## #deepseek NEXT: dprompt-27b — Query Redesign (Graph + Hierarchy) — 2026-05-10

### Task:
Implement `_graph_traverse()` and `_hierarchy_expand()` functions in `/query` endpoint to replace scope-layer filtering with two orthogonal traversal systems.

### Context:

dprompt-24/25 implemented a scope-layer model (layer 1/2/3/4 filtering queries). This was wrong and has been reverted (scratch.md documents revert completion). The correct architecture (dprompt-26) uses two separate concerns:

1. **Graph traversal** (connectivity) — finds relevant entities (who I'm connected to)
2. **Hierarchy expansion** (composition + classification) — enriches entities with taxonomic context (what they are, what they belong to)

Query now flows: graph traverse → hierarchy expand → fetch facts → merge/score → inject.

dprompt-27.md contains the technical spec. This prompt asks you to code it.

### Constraints:

- Wrong: Cascade queries filtering by layer scope (old dprompt-24 approach)
- Right: Graph finds connections; hierarchy finds classification and composition
- MUST: Read dprompt-26.md to understand correct architecture before touching code
- MUST: Implement both `_graph_traverse()` and initial query loop integration
- MUST: Keep existing baseline facts, Qdrant search, and named entity resolution unchanged
- MAY: Add logging to trace traversal steps for debugging

### Sequence (DO NOT skip or reorder):

1. Read dprompt-26.md to understand graph vs hierarchy distinction
2. Read dprompt-27.md spec (lines 58–89) for function signatures and algorithms
3. Add `_REL_TYPE_GRAPH` frozenset to main.py (line ~60, near other constants)
4. Add `_REL_TYPE_HIERARCHY` frozenset to main.py (immediately after `_REL_TYPE_GRAPH`)
5. Implement `_graph_traverse(user_id, max_hops=1)` function
   - Single-hop by default (covers spouse, children, pets, location)
   - Returns set of connected entity UUIDs
6. Rewrite `/query` endpoint loop:
   - BEFORE fetching facts, call `_graph_traverse(user_id, max_hops=1)` to get relevant entities
   - REPLACE the old cascade logic (deleted in dprompt-24/25 revert) with: `relevant_entities = _graph_traverse(...)`
   - Keep baseline facts fetch unchanged
   - Keep Qdrant search unchanged
   - Keep existing merge/deduplicate/score logic unchanged
7. Test: "where do mars and fraggle live?" returns both entities

### Deliverable:

- `_REL_TYPE_GRAPH` frozenset: spouse, parent_of, child_of, sibling_of, has_pet, knows, friend_of, met, works_for, lives_at, lives_in, located_in, owns, educated_at, member_of, pref_name, also_known_as, same_as, age, height, weight, born_on, nationality, has_gender, occupation
- `_REL_TYPE_HIERARCHY` frozenset: instance_of, subclass_of, part_of, is_a
- `_graph_traverse(user_id, max_hops=1)` function that returns set of connected entity UUIDs
- `/query` loop integration: graph traverse finds relevant entities before fact fetch
- No changes to baseline facts, Qdrant, or merge logic

### Files to Modify:

- `src/api/main.py` — Add constants and `_graph_traverse()` function; rewrite query loop to call graph traversal

### Success Criteria:

- Test: `/query` with text "where do mars and fraggle live?" returns both mars and fraggle entities (graph finds both via spouse → has_pet chain)
- Test: Baseline facts + graph results + Qdrant results merge correctly, no dupes
- Code parses cleanly: `python -m py_compile src/api/main.py`
- No regressions: existing tests still pass

### Upon Completion:

**Update scratch.md with this entry:**
```
## ✓ DONE: dprompt-27b (Graph Traversal) — 2026-05-10

- Added _REL_TYPE_GRAPH and _REL_TYPE_HIERARCHY frozensets
- Implemented _graph_traverse(user_id, max_hops=1)
- Rewrote /query loop to call graph traversal before fact fetch
- Test scenario "where do mars and fraggle live?" verified ✓
- Baseline facts, Qdrant, and merge logic unchanged
- Ready for dprompt-28b (hierarchy expansion) — see scratch for next steps
```

Then add a NEW section:

```
## #deepseek NEXT: dprompt-28b (in scratch)

[contents of dprompt-28b.md will be provided once dprompt-27b is complete]
```
