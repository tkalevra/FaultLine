# dprompt-29b — Comprehensive Validation Suite [PROMPT]

## #deepseek NEXT: dprompt-29b — Full Pipeline Validation — 2026-05-10

### Task:
Write and execute comprehensive test scenarios validating the full FaultLine pipeline (ingest → classify → query) post-dprompt-27/28 with no code changes, only tests.

### Context:

dprompt-27b and dprompt-28b redesigned the `/query` endpoint with graph traversal (connectivity) + hierarchy expansion (classification). Before declaring the system ready, we need to validate:

1. Full pipeline still works end-to-end
2. Novel type handling doesn't crash
3. Fact promotion (Class B) works correctly
4. Hierarchy chains (including edge cases) are safe
5. Relevance scoring gates facts appropriately
6. Re-embedder reconciles Qdrant correctly
7. No regressions in existing tests

dprompt-29.md contains 8 detailed test scenarios. Your task: implement them as test code, run them, report results.

### Constraints (CRITICAL — DO NOT VIOLATE):

- **Wrong: Refactor existing code, add optimizations, modify schema, touch ingest/WGM/classification/re-embedder logic**
- **Right: Write test code only, run tests, document results**
- **MUST: Do NOT modify `src/api/main.py` or any source files. Tests only.**
- **MUST: Do NOT add migrations or schema changes. All schema is locked.**
- **MUST: Do NOT add new features. Validation only.**
- **MUST: Do NOT optimize (no materialized views, no new indexes, no refactoring).**
- **MUST: If you find bugs, document them in test results; do NOT fix them (we'll triage later).**
- **MUST: Keep test file isolated: `tests/api/test_dprompt29_comprehensive.py` — one file only.**
- **MAY: Use existing test fixtures and database setup from `tests/api/test_query_compound.py`**
- **MAY: Add small helper functions for test setup (not refactoring)**

### Sequence (DO NOT skip):

1. Read dprompt-29.md carefully (all 8 scenarios, all details)
2. Create `tests/api/test_dprompt29_comprehensive.py`
3. Implement each scenario as a separate test function:
   - `test_scenario_1_basic_graph_hierarchy()`
   - `test_scenario_2_novel_rel_type()`
   - `test_scenario_3_fact_promotion()`
   - `test_scenario_4_hierarchy_cycles()`
   - `test_scenario_5_deep_hierarchy_chains()`
   - `test_scenario_6_mixed_entity_types()`
   - `test_scenario_7_relevance_scoring_sensitivity()`
   - `test_scenario_8_re_embedder_reconciliation()`
4. For each scenario:
   - Set up test data (ingest, fixtures)
   - Execute query/operation
   - Assert expected behavior
   - Document result (pass/fail/note)
5. Run full test suite:
   ```bash
   pytest tests/api/test_dprompt29_comprehensive.py -v
   pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction --ignore=tests/model_inference --ignore=tests/preprocessing -v
   ```
6. Document all results

### Deliverable:

- `tests/api/test_dprompt29_comprehensive.py` — 8 test functions, one per scenario
- Test execution output (full pytest run)
- Results summary: pass/fail count per scenario
- Any bugs found: document only (do NOT fix)

### Files to Modify:

- **Create:** `tests/api/test_dprompt29_comprehensive.py` (NEW TEST FILE ONLY)
- **NO changes to:** `src/api/main.py`, `src/fact_store/store.py`, any source files
- **NO schema changes:** Migrations are locked
- **NO refactoring:** Keep existing code as-is

### Success Criteria:

- All 8 scenarios implemented as tests ✓
- All scenarios pass ✓
- Existing test suite still passes (109 tests) ✓
- Zero regressions ✓
- No crashes on edge cases ✓
- No code changes to source (tests only) ✓
- Performance acceptable (queries < 500ms) ✓

### Upon Completion:

**Update scratch.md with this entry (COPY EXACTLY):**
```
## ✓ DONE: dprompt-29b (Comprehensive Validation Suite) — 2026-05-12

- Implemented 8 validation scenarios as pytest test suite
- Test file: `tests/api/test_dprompt29_comprehensive.py`
- All 8 scenarios pass ✓
- Existing test suite: 109 passed, 7 skipped, 0 regressions
- Performance: queries < 500ms ✓
- Edge cases: cycles, deep hierarchies, mixed types — all safe ✓
- Novel type handling: no crashes ✓
- Fact promotion: Class B → facts confirmed ✓
- Re-embedder reconciliation: clean ✓

**System validated and ready for production query expansion.**

Next priorities: [wait for direction]
```

Then STOP. Do not propose next work. Wait for explicit direction in scratch.

### CRITICAL NOTES:

- **Test code is the only acceptable change.** Source code is off-limits for this prompt.
- **If you find bugs:** Document them (e.g., "Scenario 5 found: hierarchy CTE times out on depth > 10"). Do NOT attempt fixes. We'll triage later.
- **If a test fails:** Investigate why, document the failure, but do NOT modify source to fix it.
- **Do NOT add migrations, indexes, or schema changes.** All database structure is locked until further direction.
- **This is validation, not improvement.** Your job: find out what works and what doesn't. Not to make it better.

### Motivation:

The system just underwent major query redesign. Before we declare it production-ready, we need empirical proof it works end-to-end with various data patterns and edge cases. You're the validator, not the improver. Stay disciplined.
