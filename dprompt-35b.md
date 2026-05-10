# dprompt-35b — Edge Case Fixes: UUID Leak, Age Calculation, Entity Attributes [PROMPT]

## #deepseek NEXT: dprompt-35b — Investigate & Fix Edge Cases — 2026-05-10

### Task:

Investigate and fix three edge cases from pre-prod testing (dprompt-34b): UUID leak in display name fallback, age miscalculation (192 stored instead of 36), and entity_attributes not surfaced to `/query` responses. Debug code, identify root causes, implement fixes, re-test.

### Context:

Pre-prod live testing (5 scenarios) revealed core pipeline works flawlessly — Gabriella bug is fixed, family entities visible. But three edge cases surfaced:

1. **UUID leak:** Query "How old am I" returned Gabriella's UUID instead of "gabriella" name
2. **Age miscalculation:** Input "born on January 15, 1990" stored age=192 instead of 36
3. **Entity attributes not surfaced:** Age stored in entity_attributes table but `/query` doesn't return it

These are blocking production readiness. Must be fixed before shipping.

### Constraints (CRITICAL):

- **Wrong: Assume root causes without investigating**
- **Right: Trace code, understand why each bug occurs, fix at source**
- **MUST: Investigate BEFORE fixing. Document root cause for each.**
- **MUST: Implement all 3 fixes in `src/api/main.py` and `src/entity_registry/registry.py`**
- **MUST: After fixes, run live testing again (dprompt-34b scenario 2 + 4) to confirm fixes work**
- **MUST: If any fix breaks other tests, revert and investigate further**
- **MUST: Syntax check: `python -m py_compile src/api/main.py` must pass**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-35.md spec (all investigation steps + solutions)

2. **Investigate Problem 1 (UUID Leak):**
   - Find `_resolve_display_names()` in `src/api/main.py` (search for function name)
   - Trace how it handles missing preferred names
   - Check: Does it fall back to non-preferred aliases? If not, that's the bug.
   - Verify: `registry.get_any_alias()` exists in `src/entity_registry/registry.py`
   - Document: Root cause and fix approach in scratch

3. **Investigate Problem 2 (Age Miscalculation):**
   - Find age extraction code (search for "born", "age", "1990" in `/ingest` or Filter)
   - Trace: How is birth year parsed? How is age calculated?
   - Test theory: 2026 - 1990 should be 36. If 192 is stored, where does 192 come from?
   - Possible causes: regex captures wrong group, year parsing error, off-by-one
   - Document: Root cause and fix approach in scratch

4. **Investigate Problem 3 (Entity Attributes Not Surfaced):**
   - Find `_fetch_user_facts()` in `/query` endpoint
   - Check: Does it query `entity_attributes` table?
   - Search: Is entity_attributes merged into response before injection?
   - If missing: That's the bug.
   - Document: Root cause and fix approach in scratch

5. **Implement Fix 1 (UUID Leak):**
   - Modify `_resolve_display_names()` or `get_preferred_name()` to fall back to `get_any_alias()`
   - Test: Gabriella should return "gabriella", not UUID

6. **Implement Fix 2 (Age Miscalculation):**
   - Fix age extraction/calculation (exact fix depends on root cause)
   - Test: "born on January 15, 1990" → age=36, not 192

7. **Implement Fix 3 (Entity Attributes Not Surfaced):**
   - Add `_fetch_entity_attributes()` call in `/query` after `_fetch_user_facts()`
   - Merge entity_attributes into facts list before scoring/injection
   - Test: "How old am I" returns age

8. **Syntax check:** `python -m py_compile src/api/main.py` — must pass cleanly

9. **Re-test (Live):**
   - Run dprompt-34b scenario 2 (Gabriella): Gabriella visible by name
   - Run dprompt-34b scenario 4 (Sensitivity + Age): Age calculated correctly, returned on explicit ask
   - Both must pass

10. **Update scratch** with completion entry (template below)

### Deliverable:

- **`src/api/main.py`** — fixes for UUID leak + entity_attributes surfacing + age calculation
- **`src/entity_registry/registry.py`** — (if changes needed to `get_any_alias()`)
- **Investigation notes** (in scratch) — root causes for all 3 bugs
- **Re-test results** (in scratch) — live testing confirms fixes work

### Files to Modify:

- `src/api/main.py` — UUID leak fix, entity_attributes fetch, age fix
- `src/entity_registry/registry.py` — (if needed)

### Success Criteria:

- UUID leak fixed: display name resolution falls back to non-preferred aliases ✓
- Age miscalculation fixed: age=36, not 192 ✓
- Entity attributes surfaced: `/query` includes scalar facts ✓
- Live re-test passes: Gabriella visible, age returned correctly ✓
- Syntax clean ✓
- No regressions from previous test suites ✓

### Upon Completion:

**If all 3 fixes work and live tests pass:**

Update scratch.md with this entry (COPY EXACTLY):
```
## ✓ DONE: dprompt-35b (Edge Case Fixes) — 2026-05-10

**Investigations completed:**

**Problem 1 (UUID Leak):**
- Root cause: `_resolve_display_names()` returned UUID when preferred name missing
- Fix: Added fallback to `registry.get_any_alias()` for non-preferred aliases
- Result: Gabriella now returned as "gabriella", not UUID ✓

**Problem 2 (Age Miscalculation):**
- Root cause: [describe what was wrong]
- Fix: [describe what was changed]
- Result: Age correctly calculated as 36, not 192 ✓

**Problem 3 (Entity Attributes Not Surfaced):**
- Root cause: `/query` didn't fetch entity_attributes table
- Fix: Added `_fetch_entity_attributes()` call, merged into facts list
- Result: "How old am I" now returns age ✓

**Live re-testing:**
- Scenario 2 (Gabriella): ✓ PASS — Gabriella visible by name
- Scenario 4 (Age + Sensitivity): ✓ PASS — Age calculated correctly, returned on explicit ask
- No regressions ✓

**System is now production-ready. All edge cases fixed.**

Next: [awaiting direction for deployment]
```

**If any fix fails or introduces regression:**

Update scratch.md with:
```
## ❌ FAILED: dprompt-35b (Edge Case Fix) — Fix X broke Y

**Issue:** [what failed]
**Root cause:** [why it failed]
**Evidence:** [test output, error message]

Awaiting direction on alternative fix or investigation.
```

Then STOP. Do not attempt further fixes. Wait for direction.

### CRITICAL NOTES:

- **Investigate first.** Don't guess. Trace the code, understand the bugs, then fix.
- **UUID leak is priority 1.** Without this, Gabriella displays as UUID on explicit queries.
- **Age bug is priority 2.** Storing 192 instead of 36 is clearly wrong; find where 192 comes from.
- **Entity attributes is priority 3.** Nice-to-have for user queries, but blocks "How old am I" functionality.
- **Live re-test is mandatory.** After fixes, test against pre-prod with actual API calls. Don't just run unit tests.
- **All 3 must work.** If any one is broken, system isn't production-ready.

### Motivation:

Gabriella bug is fixed. Core pipeline works. But these edge cases block shipping. UUID leak is a UX killer (users see UUIDs). Age miscalculation is nonsensical. Entity attributes not surfacing breaks natural queries. Fix all 3, re-test, and system is ready to ship.

