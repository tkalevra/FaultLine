# dprompt-33 — Full-Path Test Suite: Integration Validation

## Purpose

Rewrite the test suite from unit-level (isolated scenarios) to **full-path integration validation** (ingest → conflict detection → re-embedder resolution → query verification). Current approach missed the Gabriella name collision bug entirely. New approach validates complete end-to-end cycles.

## The Problem

**dprompt-29b (8 scenarios)** and **dprompt-30b (15 QA scenarios)** both passed with 0 regressions. System marked PRODUCTION-READY. But live testing revealed **critical integration failure**: Gabriella's entity ingested successfully, but invisible in queries due to name collision with user's preferred name.

**Root cause of gap:** Unit tests validated components independently:
- Ingest tests: "facts are stored" ✓
- Query tests: "facts are retrieved" ✓
- But NOT: "ingest → name collision detected → re-embedder resolves → query returns entity" ✓

Full-path testing catches these integration failures.

## Solution Architecture

**Test structure:** Each scenario follows complete cycle:
1. **Setup** — seed entities/facts (or clear and start fresh)
2. **Ingest** — feed user message(s) through pipeline (LLM extraction, WGM gate, fact classification)
3. **Collision Check** — verify `entity_name_conflicts` table populated if collision occurs
4. **Re-Embedder Cycle** — run conflict resolution (LLM evaluation, alias updates, conflict marked resolved)
5. **Query Verification** — execute `/query`, validate expected entities + facts returned
6. **Assertions** — all entities visible, preferred names correct, no data loss

## Test Scenarios (23 total)

### Group A: Base Integration (5 scenarios from dprompt-30b refactored as full-path)

1. **Complex Family Prose** — "We have three kids: Cyrus, Desmonde, and Gabriella (Gabby, age 10)" → ingest → graph traverse → query returns all 4 family members with correct facts
2. **Complex System Metadata** — hostname, IP, OS, SSL expiry facts → ingest → query context retrieval
3. **Fact Correction Cycle** — ingest old age → correct age → query returns updated age only
4. **Alias Resolution Under Query** — "also_known_as" aliases → ingest → query with different names → correct entity returned
5. **Fact Promotion (Class B)** — behavioural fact → confirm 3x → promoted to facts → re-embedder syncs → query includes promoted fact

### Group B: Name Collision + Resolution (6 new scenarios)

6. **Simple Name Collision** — two entities, same pref_name → ingest both → collision detected and stored pending
7. **Collision Resolution via LLM** — pending collision → re-embedder cycle → LLM resolves winner/loser → aliases updated, conflict marked resolved
8. **Gabriella Reproduction** — exact scenario from live bug: "We have a third Daughter, Gabriella who's 10 and goes by Gabby" → user already has pref_name="gabby" → collision detected → LLM assigns fallback "gabriella" to child → query returns Gabriella with parent_of fact ✓
9. **Triple Collision (Three Entities)** — three entities claim same name → conflicts table has two entries → re-embedder resolves both → all three visible with unique display names
10. **Collision with Scalar Facts** — name collision + missing scalar facts (age, occupation) → re-embedder resolves + age fact ingested → query returns complete entity
11. **Resolved Collision Handling** — re-ingest same entities → no new conflicts (already resolved) → query returns consistent results

### Group C: Hierarchy + Graph Integration (4 scenarios)

12. **Graph + Hierarchy Full Path** — ingest Mars (spouse) + Fraggle (pet, animal type) → graph traverse finds connected, hierarchy expand finds animal classification → query returns Mars + Fraggle + classification chain
13. **Hierarchy Depth with Collision** — deep hierarchy (animal → mammal → dog → poodle) + name collision in leaf node → LLM resolves with hierarchy context → correct entity visible at all layers
14. **Mixed Entity Types in Query** — Person, Animal, Organization entities → ingest relationships spanning types → graph traversal respects types → query returns all with correct constraints
15. **Transitive Hierarchy Discovery** — ingest "my kid is student → student is person" → hierarchy infers "my kid is person" → query reflects transitive membership

### Group D: Sensitivity + Novel Types (4 scenarios)

16. **Sensitive Fact Gating** — ingest birthday + address (sensitive) → query without explicit ask → facts filtered → query with "how old" → facts returned
17. **Novel Rel_Type Full Path** — unknown rel_type ingested → stored as Class C → re-embedder evaluates → if similar to existing (cosine > 0.85) → mapped; else rejected → query handles gracefully
18. **Confidence Variation** — facts with varying confidence (0.4 → 0.8) → ingest → re-embedder promotion logic respects confidence → query scores appropriately
19. **Unknown Entity Type Propagation** — entity type = "unknown" at ingest → later ingest with clear type → entity_type updated → query uses updated type

### Group E: Idempotency + Edge Cases (4 scenarios)

20. **Duplicate Ingest (10x)** — same fact ingested 10 times → ingest idempotent → facts table has 1 entry, confirmed_count = 10 → query returns once ✓
21. **Partial Re-Ingest** — ingest facts A, B, C → re-ingest A, B (C skipped) → facts unchanged, query consistent
22. **Circular Relationships (Defensive)** — entity A → is_part_of → B → is_part_of → A (cycle) → re-embedder CTE handles gracefully with depth limit → query doesn't hang or duplicate
23. **Empty Query / No Matching Facts** — query with no matching facts → returns empty list gracefully (no error)

## Schema Assumptions

- `entity_name_conflicts` table exists (dprompt-32b)
- `facts`, `staged_facts`, `entity_aliases` populated correctly
- Re-embedder function `resolve_name_conflicts()` implemented
- LLM endpoint responds with JSON conflict resolution

## Test File Structure

**Location:** `tests/api/test_suite_full_path.py`

**Pattern per scenario:**
```python
def test_scenario_N_<name>():
    """
    Full path: [setup] → [ingest] → [collision check if applicable] → 
    [re-embedder cycle] → [query verification] → [assertions]
    """
    # Setup
    # Ingest
    # Collision check (if applicable)
    # Re-embedder cycle (simulate or run actual background process)
    # Query verification
    # Assert all expected entities + facts returned
```

**Fixtures required:**
- `db_conn` — PostgreSQL connection (skip test if POSTGRES_DSN not set)
- `embedder_mock` or `embedder_runner` — re-embedder callable
- `query_endpoint` — callable /query endpoint

**Timeouts:**
- Collision resolution: 5s (LLM call)
- Query with deep hierarchy: 10s
- Re-embedder cycle: 10s

**Assertions:**
- No data loss (all ingested facts present)
- Name collisions detected + resolved
- Query returns expected entities + facts
- Preferred names correct (winner gets disputed name, loser gets fallback)
- Non-preferred aliases preserved (only preferred status changes)

## Success Criteria

- All 23 scenarios pass ✓
- No regressions from dprompt-29b/30b test suite ✓
- Gabriella scenario (test 8) reproduces bug, confirms fix ✓
- Full cycle timing reasonable (< 60s per test on standard hardware) ✓
- Test file parses cleanly ✓
- Coverage: ingest, collision, resolution, query, sensitivity, novel types, hierarchy, edge cases ✓

## Files to Modify

| File | Change |
|------|--------|
| `tests/api/test_suite_full_path.py` | NEW — 23 full-path integration scenarios |

## References

- `dprompt-30.md` — base 15 QA scenarios (refactor as full-path cycles)
- `dprompt-32.md` — conflict resolution spec (collision scenarios)
- `CLAUDE.md` — fact classification, relevance scoring, re-embedder behavior

