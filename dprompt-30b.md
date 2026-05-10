# dprompt-30b — QA Stress Suite: Real-World Extraction & Query [PROMPT]

## #deepseek NEXT: dprompt-30b — QA Stress Testing (after dprompt-29b passes) — 2026-05-10

### Task:
Write and execute comprehensive QA tests for real-world usage patterns: complex natural language extraction, family relationships, multi-attribute objects, fact corrections, sensitive info gating, and edge cases.

### Context:

dprompt-29b validated that the core system didn't break. dprompt-30b stress-tests that it actually works for real usage—the stuff users will throw at it:

- "My wife and I have three kids..." (natural prose with aliases)
- "My server is prod-api-01.acme.com, IP 10.0.1.42..." (technical multi-attribute objects)
- "I'm 30... wait, I'm 31... actually 30" (triple corrections)
- "Where do I live?" vs "Tell me about me" (sensitive gating with explicit overrides)
- Novel rel_types, deep graph queries, circular relationships (edge cases)

dprompt-30.md contains 15 detailed QA scenarios. Your task: implement them as pytest tests, run them, report results.

**Goal:** Empirical proof the system is production-ready ("shippable and doesn't stall at the starting line").

### Constraints (CRITICAL — IDENTICAL TO dprompt-29b):

- **Wrong: Refactor code, add optimizations, modify schema, touch ingest/WGM/classification logic**
- **Right: Write test code only, run tests, document results**
- **MUST: Do NOT modify `src/api/main.py` or any source files. Tests only.**
- **MUST: Do NOT add migrations or schema changes.**
- **MUST: Do NOT add new features. QA only.**
- **MUST: If you find bugs, document them; do NOT fix them.**
- **MUST: Keep test file isolated: `tests/api/test_dprompt30_qa_suite.py` — one file only.**
- **MAY: Use existing test fixtures from `test_dprompt29_comprehensive.py` if helpful**

### Sequence (DO NOT skip):

1. Wait for dprompt-29b to complete and be committed (explicit validation that 29 passed)
2. Read dprompt-30.md carefully (all 15 scenarios)
3. Create `tests/api/test_dprompt30_qa_suite.py`
4. Implement each scenario as a separate test function:
   - `test_scenario_1_complex_family_prose()`
   - `test_scenario_2_complex_system_metadata()`
   - `test_scenario_3_alias_resolution_under_query()`
   - `test_scenario_4_age_update_fact_supersede()`
   - `test_scenario_5_relationship_change_spouse_update()`
   - `test_scenario_6_triple_correction()`
   - `test_scenario_7_mixed_sensitive_query()`
   - `test_scenario_8_birthday_gating()`
   - `test_scenario_9_unknown_rel_type_graceful_degradation()`
   - `test_scenario_10_extended_family_transitive_discovery()`
   - `test_scenario_11_my_kids_auto_discovery()`
   - `test_scenario_12_three_hop_transitive_query()`
   - `test_scenario_13_duplicate_ingest()`
   - `test_scenario_14_partial_re_ingest()`
   - `test_scenario_15_circular_relationships_defensive()`
5. For each scenario:
   - Set up test data (ingest, fixtures)
   - Execute operation
   - Assert expected behavior
   - Document result (pass/fail/note)
6. Run full test suite:
   ```bash
   pytest tests/api/test_dprompt30_qa_suite.py -v
   pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction --ignore=tests/model_inference --ignore=tests/preprocessing -v
   ```
7. Measure performance baselines (query time, reconciliation time)
8. Document all results

### Deliverable:

- `tests/api/test_dprompt30_qa_suite.py` — 15 test functions, one per scenario
- Test execution output (full pytest run)
- Results summary: pass/fail count per scenario
- Performance measurements (query time, etc.)
- Any bugs found: document only (do NOT fix)
- Explicit statement: "System is [READY / NOT READY] for production"

### Files to Modify:

- **Create:** `tests/api/test_dprompt30_qa_suite.py` (NEW TEST FILE ONLY)
- **NO changes to:** `src/api/main.py`, `src/fact_store/store.py`, any source files
- **NO schema changes:** Migrations are locked
- **NO refactoring:** Keep existing code as-is

### Success Criteria:

- All 15 scenarios implemented as tests ✓
- All 15 scenarios pass ✓
- dprompt-29b tests still pass (no regressions) ✓
- Zero source code changes ✓
- Performance baselines measured and acceptable ✓
- Bugs (if any) documented clearly ✓
- Explicit readiness statement: production-ready or not ✓

### Upon Completion:

**Update scratch.md with this entry (COPY EXACTLY):**
```
## ✓ DONE: dprompt-30b (QA Stress Suite) — 2026-05-12

- Implemented 15 real-world QA scenarios as pytest test suite
- Test file: `tests/api/test_dprompt30_qa_suite.py`
- All 15 scenarios pass ✓
- dprompt-29b tests still pass ✓
- Performance baselines: [query <500ms, hierarchy <200ms, reconciliation <5s]
- Bugs found: [none / list if any, with notes]
- System readiness: PRODUCTION-READY ✓

**FaultLine is shippable.**
```

Then STOP. Do not propose next work. Wait for explicit direction in scratch.

### CRITICAL NOTES:

- **Test code only.** Source code is off-limits.
- **Do NOT optimize.** Performance baselines are informational; if slow, document it (don't add indexes or refactor).
- **Do NOT fix bugs.** Document them for triage.
- **Wait for dprompt-29b completion.** Don't start dprompt-30b until 29b is committed.
- **This is go/no-go testing.** You're answering: "Is this ready to ship?" Not "Can we make it better?"

### Motivation:

You've built a complex personal knowledge graph system. Before shipping it to production, we need empirical proof it handles real messy data, aliases, corrections, sensitive info, and edge cases without breaking. This suite answers that question. You're the QA gate-keeper. Be thorough, be honest about what works and what doesn't.
