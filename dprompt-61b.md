# dprompt-61b: DEEPSEEK_INSTRUCTION_TEMPLATE — Query Deduplication via is_preferred

## Task

Implement query deduplication in `/query` endpoint to return single facts per relationship with alias metadata, eliminating duplicate facts caused by entity alias redundancy.

## Context

`dBug-report-005` found that query results contain duplicate facts when entities have multiple aliases. Example: user has christopher/chris aliases. Query returns both `christopher spouse mars` AND `chris spouse mars` as separate facts. Expected: single deduplicated fact with alias context metadata.

**Why:** Improves query UX (no duplicate information), respects data integrity (graph/hierarchy unchanged), and prepares data for natural language rendering with fallback aliases.

**Integration:** Solution must work alongside graph traversal and hierarchy expansion. Deduplication happens AFTER all facts are collected (baseline + graph + hierarchy + Qdrant), not during collection.

**Reference:** Read `dprompt-61.md` (specification), `BUGS/dBug-report-005.md` (investigation), `CLAUDE.md` (is_preferred semantics).

## Constraints

### MUST:
- Deduplicate facts by `(subject_id, rel_type, object_id)` — keep one fact per triple
- Use `is_preferred=true` aliases as subject/object display names in returned facts
- Include `_aliases` metadata showing all entity names with is_preferred flag (both subject and object)
- Preserve all graph traversal results (connectivity unchanged)
- Preserve all hierarchy chains (instance_of/subclass_of/part_of/member_of/is_a fully intact)
- Return display names only (never expose UUIDs) in facts
- Pass all existing tests (114+ in suite, 0 new failures)

### DO NOT:
- Modify PostgreSQL schema or fact storage
- Change how facts are collected (all sources still queried: baseline, graph, hierarchy, Qdrant)
- Break Qdrant synchronization or vector search
- Expose entity_id UUIDs in API response
- Filter facts during collection (only deduplicate after merged)

### MAY:
- Add new test cases for deduplication scenarios (alias redundancy, hierarchy with aliases)
- Include performance notes in code comments if deduplication introduces complexity

## Sequence

### 1. Read & Understand (No coding)

- Read `dprompt-61.md` (specification, examples, scope)
- Read `BUGS/dBug-report-005.md` (bug investigation, test cases)
- Read `CLAUDE.md` section on `/query` path, entity aliases, is_preferred semantics
- Read `src/api/main.py` — locate `/query` endpoint, understand fact collection order:
  1. `_fetch_user_facts()` UNION (baseline facts from facts + staged_facts)
  2. `_graph_traverse()` (single-hop connectivity)
  3. `_hierarchy_expand()` (composition chains)
  4. Qdrant vector search (similarity)
  5. `_attributes_to_facts()` (scalar attributes)
  6. Merge + deduplicate (WHERE YOU ADD CODE)
  7. Return to Filter

- Confirm: Where in the sequence should deduplication happen? (After merge, before return)

### 2. Identify Code Location

**File:** `src/api/main.py`, `/query` endpoint

**Locate:** The section that merges results from multiple sources (baseline + graph + hierarchy + Qdrant + attributes).

**Pattern:** Look for `merged_facts = ...` or similar, where all fact sources are combined.

**Task:** Add deduplication + alias metadata building at THIS POINT (after merge, before return).

### 3. Implement Deduplication Logic

**Logic (pseudo-code — you implement):**

```
For each fact in merged_facts:
  1. Check if (subject_id, rel_type, object_id) already seen
  2. If NOT seen: keep fact, resolve subject/object to is_preferred alias
  3. If SEEN: compare confidence, keep higher-conf version
  4. For all facts, build _aliases dict:
     - Query: all aliases for subject_id where is_preferred=true/false
     - Query: all aliases for object_id where is_preferred=true/false
     - Attach as fact["_aliases"] = {"subject": [...], "object": [...]}
```

**Key:** subject/object in returned facts MUST be is_preferred display names (strings), not UUIDs.

**Alias metadata query:** Use existing `entity_aliases` table joins from `/query` logic. Build parallel dict while deduplicating.

### 4. Test Locally

**Run existing test suite:**
```bash
pytest tests/api/test_query.py -v
```

Expected: All 114+ tests pass, 0 regressions.

**Spot-check scenario (dBug-report-005 example):**

```bash
# POST /query with user_id="<user_uuid>"
# Verify response includes:
# - One fact: {"subject": "user", "rel_type": "spouse", "object": "mars", ...}
# - _aliases metadata: {"subject": [...], "object": [...]}
# - No duplicate facts for christopher/chris aliases
```

### 5. Validate Integration

**Graph traversal:** Run a test that queries a connected entity (e.g., spouse) and verify:
- Graph traversal still returns all connected entities
- Facts are deduplicated but complete

**Hierarchy expansion:** Run a test with hierarchical entities (e.g., fraggle instance_of morkie instance_of dog) and verify:
- Full hierarchy chain returned
- Facts deduplicated but chains intact

**Example test:**
```
Query: /query?user_id=user
Response should include:
- fraggle instance_of morkie (with _aliases for both)
- morkie instance_of dog (with _aliases for both)
- user has_pet fraggle (with _aliases)
Not:
- Duplicate facts due to aliases
- Broken chains
```

### 6. STOP & Report

Update `scratch.md` with template below. Do NOT proceed to deployment or live testing.

## Deliverable

**Modified file:** `src/api/main.py`, `/query` endpoint

- Added deduplication logic after fact merging (before return to Filter)
- Facts deduplicated by (subject_id, rel_type, object_id)
- Subject/object resolved to is_preferred aliases (display names)
- `_aliases` metadata attached to all facts
- No schema changes, no fact storage changes

**New test cases:** `tests/api/test_query.py`

- Test: Multiple aliases → single deduplicated fact
- Test: Hierarchy with aliases → chains intact, facts deduplicated
- Test: _aliases metadata structure

## Files to Modify

```
src/api/main.py
├─ /query endpoint
│  └─ Add deduplication + _aliases metadata building (~60–100 lines)
│     Location: After fact merge (baseline + graph + hierarchy + Qdrant + attributes)
│     Before: return fact list to Filter

tests/api/test_query.py
└─ Add 3–4 test cases for deduplication scenarios
```

## Success Criteria

✅ Deduplication: One fact per (subject_id, rel_type, object_id) triple  
✅ Aliases: Subject/object use is_preferred=true display names (strings, not UUIDs)  
✅ Metadata: `_aliases` dict with all aliases + is_preferred flag for both entities  
✅ Graph: Traversal results unchanged, connectivity intact  
✅ Hierarchy: All chains (instance_of/subclass_of/part_of/member_of/is_a) fully returned  
✅ Tests: 114+ pass, 0 regressions, 3–4 new test cases passing  
✅ No UUIDs in response facts (display names only)  

## Upon Completion

**⚠️ MANDATORY: Update scratch.md (FaultLine-dev) with this template, then STOP:**

```markdown
## ✓ DONE: dprompt-61 (Query Deduplication via is_preferred) — [DATE]

**Task:** Implement deduplication in /query to return single facts per relationship with alias metadata.

**Implementation (src/api/main.py):**
- Location: `/query` endpoint, after fact merging, before return
- Deduplication: by (subject_id, rel_type, object_id)
- Alias resolution: subject/object use is_preferred=true display names
- Metadata: _aliases dict with all aliases + preference flag
- Lines: [START_LINE] → [END_LINE] (+[N] lines)

**Tests (tests/api/test_query.py):**
- Test: Multiple aliases → single fact
- Test: Hierarchy with aliases → chains intact
- Test: _aliases metadata structure
- All 114+ existing tests pass ✓

**Validation:**
- Deduplication verified: one fact per (subject_id, rel_type, object_id)
- Graph traversal: connectivity unchanged
- Hierarchy: all chains intact (instance_of/subclass_of/part_of/member_of/is_a)
- No UUIDs exposed in response

**Example result:**
- Query: "Tell me about my family"
- Spouse fact: {"subject": "user", "rel_type": "spouse", "object": "mars", "_aliases": {"subject": [...], "object": [{"name": "mars", "is_preferred": true}, {"name": "marla", "is_preferred": false}]}}
- Natural language: "Your spouse is Mars (also known as Marla)"

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, do not deploy, wait for user direction.

## Critical Rules (Non-Negotiable)

**Deduplication timing:** Happens AFTER all facts collected (baseline + graph + hierarchy + Qdrant), not during. This preserves graph/hierarchy structure.

**Alias resolution:** Display names only in returned facts. UUIDs stay in subject_id/object_id columns (PostgreSQL), not in API response.

**Graph/Hierarchy integrity:** Zero changes to traversal logic. Deduplication is a presentation layer (API response), not a data layer change.

**Test discipline:** 114+ existing tests must pass. New tests verify deduplication + metadata. Zero regressions.

**STOP clause is mandatory:** Every implementation ends with STOP. User must rebuild pre-prod, validate, then decide next steps.

---

**Template version:** 1.0 (follows PRODUCTION_DEPLOYMENT_GUIDE.md + DEEPSEEK_INSTRUCTION_TEMPLATE)  
**Status:** Ready for execution by deepseek
