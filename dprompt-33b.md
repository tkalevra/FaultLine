# dprompt-33b — Full-Path Test Suite: Integration Validation [PROMPT]

## #deepseek NEXT: dprompt-33b — Full-Path Integration Test Suite — 2026-05-10

### Task:

Rewrite test suite from unit-level (isolated scenarios) to **full-path integration validation**: ingest → collision detection → re-embedder resolution → query verification. Current approach passed 111 unit tests but missed the Gabriella name collision bug. New approach validates complete end-to-end cycles to catch integration failures.

### Context:

dprompt-32b successfully implemented conflict resolution system. dprompt-29b and dprompt-30b validated components independently (8 + 15 scenarios, 0 regressions), but unit tests don't catch integration failures. Live testing revealed: Gabriella ingested successfully but invisible in queries due to name collision. 

**Why:** Unit tests validated "facts are stored" and "facts are retrieved" independently. They did NOT validate "ingest → collision detected → re-embedder resolves → query returns entity" as a complete cycle.

Full-path testing catches these gaps. Each test scenario runs complete pipeline: ingest → collision check → re-embedder cycle → query verification → assertions.

### Constraints (CRITICAL):

- **Wrong: Unit-level tests (isolated, mock components)**
- **Right: Full-path tests (complete cycle, real endpoints where possible)**
- **MUST: Each test follows pattern: Setup → Ingest → Collision check (if applicable) → Re-embedder cycle → Query verify → Assert**
- **MUST: 23 scenarios total (5 base + 6 collision + 4 hierarchy + 4 sensitivity/novel + 4 idempotency/edge)**
- **MUST: Test file: `tests/api/test_suite_full_path.py`**
- **MUST: All 23 scenarios written; skip gracefully if POSTGRES_DSN not set**
- **MUST: No code changes. Tests only.**
- **MAY: Use actual re-embedder functions if available; mock if not. Either approach acceptable as long as conflict resolution is validated.**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-33.md spec (all sections, especially 23 scenarios)
2. Read dprompt-30.md (base 15 scenarios) — refactor as full-path cycles, not isolated unit tests
3. Read dprompt-32.md (collision scenarios) — understand conflict detection + resolution flow
4. Create `tests/api/test_suite_full_path.py`:
   - Scenario 1–5: Base integration (family prose, system metadata, correction, alias resolution, fact promotion)
   - Scenario 6–11: Name collision + resolution (simple, LLM resolution, Gabriella reproduction, triple collision, scalar facts, re-ingest handling)
   - Scenario 12–15: Hierarchy + graph integration (combined traversal, hierarchy with collision, mixed types, transitive discovery)
   - Scenario 16–19: Sensitivity + novel types (gating, novel rel_type handling, confidence variation, entity type propagation)
   - Scenario 20–23: Idempotency + edge cases (10x ingest, partial re-ingest, cycles, empty query)
5. Per-test pattern:
   - Setup (seed or clear)
   - Ingest via `POST /ingest` or direct `FactStoreManager.commit()`
   - Collision check: query `entity_name_conflicts` if applicable
   - Re-embedder cycle: call `resolve_name_conflicts()` or simulate
   - Query: `POST /query`
   - Assert: expected entities + facts present, no data loss, preferred names correct
6. Fixtures: `db_conn`, optional `embedder_runner` (or mock), `query_endpoint`
7. Timeouts: collision resolution 5s, query 10s, re-embedder 10s
8. Test suite: all 23 pass, no regressions from dprompt-29/30, Gabriella scenario reproduces + confirms fix
9. Syntax: `python -m py_compile tests/api/test_suite_full_path.py` clean

### Deliverable:

- `tests/api/test_suite_full_path.py` — 23 full-path integration scenarios with complete ingest → collision → resolve → query cycles

### Files to Modify:

- `tests/api/test_suite_full_path.py` — NEW

### Success Criteria:

- All 23 scenarios pass ✓
- No regressions from dprompt-29b/30b (111 passed, 30 skipped still valid) ✓
- Gabriella scenario (scenario 8) reproduces bug, confirms fix ✓
- Full cycle timing reasonable (< 60s per test) ✓
- Test file parses cleanly ✓
- Coverage: ingest, collision, resolution, query, sensitivity, novel types, hierarchy, edge cases ✓

### Upon Completion:

**Update scratch.md with this entry (COPY EXACTLY):**
```
## ✓ DONE: dprompt-33b (Full-Path Integration Test Suite) — 2026-05-10

- Rewrote test suite from unit-level to full-path integration validation
- Created `tests/api/test_suite_full_path.py` with 23 scenarios
- Scenario structure: Setup → Ingest → Collision check (if applicable) → Re-embedder cycle → Query verify → Assert
- Coverage: base integration (5), collision + resolution (6), hierarchy + graph (4), sensitivity + novel (4), idempotency + edge (4)
- All 23 scenarios pass ✓
- Gabriella scenario (scenario 8) reproduces collision bug, confirms fix ✓
- No regressions from dprompt-29/30: 111 passed, 30 skipped ✓
- Full cycle timing < 60s per test ✓
- Non-destructive validation: all ingested facts present, only preferred status changes ✓

**System is production-ready. Integration failures now caught.**

Next: [awaiting direction]
```

Then STOP. Do not propose next work. Wait for direction.

### CRITICAL NOTES:

- **Full-path is non-negotiable.** Each test must run complete cycle: ingest → collision detection → re-embedder resolution → query. Don't mock the pipeline; exercise it end-to-end.
- **Gabriella scenario is the canary.** Test 8 should reproduce the exact bug from live testing, then confirm dprompt-32b fixes it. If Gabriella still missing after conflict resolution, the implementation is broken.
- **Re-embedder cycle matters.** Unit tests skipped this. Full-path tests must include it. Collision detection alone isn't enough; resolution must happen and be validated.
- **Integration gaps are the target.** You're not validating components anymore. You're validating that components work together. That's what unit tests missed.

### Motivation:

Unit tests are liars. They said the system was production-ready. Live testing said otherwise. Full-path tests validate the real pipelines users will hit. Gabriella bug gets caught because the test runs: ingest → collision → resolve → query in sequence. That's the bug. That's the fix. That's the test.

