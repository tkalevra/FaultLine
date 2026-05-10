# dprompt-34b — Pre-Production Validation: Full-Path Tests Against Live Instance [PROMPT]

## #deepseek NEXT: dprompt-34b — Pre-Prod Validation (Full-Path Test Suite) — 2026-05-10

### Task:

Run the full-path integration test suite (dprompt-33b) against the pre-production PostgreSQL instance on truenas with a fresh, wiped database. Validate that all 23 scenarios pass, confirming end-to-end pipeline works: ingest → collision detection → re-embedder resolution → query.

### Context:

Database on truenas has been wiped (schema dropped and recreated). Full-path test suite is ready (`tests/api/test_suite_full_path.py`, 23 scenarios). System is marked production-ready by unit tests, but integration failures were caught by full-path testing (Gabriella name collision bug). This validation confirms the fix works end-to-end against the live instance.

**Why:** Unit tests validated components independently. Pre-prod testing validates the complete pipeline with real data, real timing, real collision detection + LLM resolution. If all 23 pass, system is production-ready.

### Constraints (CRITICAL):

- **Wrong: Mock the database or skip collision detection testing**
- **Right: Run all 23 full-path scenarios against actual PostgreSQL on truenas**
- **MUST: Use SSH tunnel for PostgreSQL: `ssh -L 5433:localhost:5432 truenas` (forwards port 5432 → local 5433)**
- **MUST: Set `POSTGRES_DSN=postgresql://faultline:faultline@localhost:5433/faultline_test`**
- **MUST: All 23 scenarios MUST pass. Zero failures.**
- **MUST: After tests pass, manually validate via `/query` endpoint: seed data → ingest facts → query "What's my family" → confirm all entities returned with correct names**
- **MUST: If any test fails, STOP and document failure in scratch (do NOT fix). Report which scenario, error message, and PostgreSQL state.**
- **MUST: Tests only. Zero code changes.**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-34.md spec carefully
2. Establish SSH tunnel: `ssh -L 5433:localhost:5432 truenas &` (run in background)
3. Verify connection: `psql -h localhost -p 5433 -U faultline -d faultline_test -c "SELECT COUNT(*) FROM pg_tables WHERE tablename='facts';" 2>&1` — should return 0 (empty schema)
4. Set env vars:
   ```bash
   export POSTGRES_DSN="postgresql://faultline:faultline@localhost:5433/faultline_test"
   export QWEN_API_URL="http://truenas:11434/v1/chat/completions"  # or use SSH tunnel if needed
   ```
5. Run full test suite: `python -m pytest tests/api/test_suite_full_path.py -v 2>&1 | tee full_path_validation.log`
6. Verify output: All 23 pass (format: "23 passed, 0 skipped, 0 failed")
7. Manual validation (if tests pass):
   - Ingest sample data via direct Python calls (e.g., `FactStoreManager.commit()`)
   - Query via `/query` endpoint with natural language ("What's my family")
   - Confirm all entities returned with preferred names (no UUIDs, no missing entities)
   - If collision detected during ingest, confirm LLM resolved it + loser got fallback alias
8. If all tests pass AND manual validation succeeds: commit log file + proceed to "Upon Completion"
9. If any test fails: document failure in scratch.md (don't fix), then STOP and wait for direction

### Deliverable:

- **`full_path_validation.log`** — pytest output (all 23 scenarios, pass/fail status)
- **Manual validation report** (in scratch upon completion) — summary of "What's my family" query and entity resolution

### Files to Modify:

- **Create:** `full_path_validation.log` (pytest output, commit to git)
- **Update:** `scratch.md` (completion entry from template below)
- **NO code changes.**

### Success Criteria:

- All 23 full-path scenarios pass ✓
- SSH tunnel connection stable (no timeout failures)
- Gabriella scenario (test 8) passes ✓
- Manual "What's my family" query returns all family entities with correct preferred names ✓
- No data loss during full-path cycles ✓
- Log file committed to git ✓

### Upon Completion:

**If all 23 tests pass:**

Update scratch.md with this entry (COPY EXACTLY):
```
## ✓ DONE: dprompt-34b (Pre-Prod Validation — Full-Path Tests) — 2026-05-10

- Set up SSH tunnel to truenas PostgreSQL (port forwarding 5433 ← 5432)
- Ran full-path integration test suite: all 23 scenarios executed
- Results: 23 passed, 0 failed ✓
- Gabriella scenario (test 8) passed: collision detected → LLM resolved → query returned entity ✓
- Manual validation: "What's my family" query returned all family members with correct preferred names ✓
- No data loss; all ingested facts preserved ✓
- Log file `full_path_validation.log` committed to git

**System is production-ready. Integration pipeline validated end-to-end.**

Next: [awaiting direction for deployment / further work]
```

**If any test fails:**

Update scratch.md with:
```
## ❌ FAILED: dprompt-34b (Pre-Prod Validation) — Test failure at scenario X

**Failure:** [scenario name, error message]

**PostgreSQL state:** [table counts, relevant data]

**Log:** [key error lines from pytest output]

Awaiting direction on fix or further investigation.
```

Then STOP. Do not attempt to fix. Wait for direction.

### CRITICAL NOTES:

- **SSH tunnel is non-negotiable.** PostgreSQL isn't exposed on TCP directly. Use port forwarding.
- **All 23 must pass.** If even one fails, the system isn't production-ready. Document and wait for direction.
- **Manual validation is required.** Tests validate components; manual validation confirms end-to-end user experience ("What's my family" should just work).
- **Gabriella is the canary.** If scenario 8 doesn't pass, the conflict resolution system is broken.
- **No code changes.** If tests fail, it's a discovery, not a fix. Report findings.

### Motivation:

Unit tests are confidence builders. Pre-prod validation is the truth test. All 23 scenarios must pass for deployment. Gabriella bug should be invisible now (collision detected + resolved by LLM). "What's my family" should return all family members with their preferred names, no UUIDs, no missing entities. That's what production-ready means.

