# dprompt-37b — Comprehensive Pre-Prod Validation: All 5 Scenarios [PROMPT]

## #deepseek NEXT: dprompt-37b — Full Pre-Prod Re-Validation — 2026-05-10

### Task:

Re-run all 5 end-to-end scenarios (dprompt-34b) against pre-prod with all fixes deployed and verified. Comprehensive validation: every scenario, every edge case, every validation rule, database state. Report complete pass/fail status before declaring system production-ready.

### Context:

All edge cases fixed and partially verified:
- UUID leak fixed (dprompt-35b / dprompt-32b)
- Age validation now entity-type-aware (dprompt-36b)
- Entity_attributes surfacing fixed (dprompt-35b)

Pre-prod docker image rebuilt with all fixes. Partial verification shows Gabriella visible by name, family queries work, age validation active. But need comprehensive 5-scenario pass to confirm system readiness.

### Constraints (CRITICAL):

- **Wrong: Spot-check only (2 scenarios). Need full validation.**
- **Right: Run all 5 scenarios end-to-end. Document every result.**
- **MUST: All 5 scenarios MUST pass. Zero failures.**
- **MUST: Database state verified after each scenario (SSH queries): facts count, conflicts table, entity_attributes, aliases)**
- **MUST: No UUID leaks in ANY query response. Not acceptable.**
- **MUST: Age validation working: Person age=36 ✓, Person age=192 ✗, Planet age=4.5B ✓**
- **MUST: Sensitivity gating working: birthday not leaked on generic query, returned on explicit ask**
- **MUST: If ANY scenario fails, STOP and report (don't fix). Document exact failure.**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-37.md spec (all 5 scenarios, validation checklist)

2. **Scenario 1: Family Ingest + Query**
   ```bash
   # Ingest
   curl -X POST -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
     -H "Content-Type: application/json" \
     -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"We have two kids: Cyrus and Desmonde, and a spouse Mars"}],"stream":false}' \
     https://hairbrush.helpdeskpro.ca/api/chat/completions
   
   # Wait 2s
   
   # Query
   curl -X POST -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
     -H "Content-Type: application/json" \
     -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"What is my family"}],"stream":false}' \
     https://hairbrush.helpdeskpro.ca/api/chat/completions
   ```
   - Expected: Response mentions Mars, Cyrus, Desmonde by NAME
   - PASS/FAIL: ___
   - Database verify: `ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT COUNT(*) FROM facts WHERE rel_type IN (\"spouse\", \"parent_of\");'"` — should be ≥ 3

3. **Scenario 2: Gabriella (The Canary)**
   ```bash
   # First: User's preferred name
   curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"I go by Gabby"}],...}'
   
   # Wait 1s
   
   # Second: Gabriella with collision
   curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"We have a third daughter Gabriella who is 10 and goes by Gabby"}],...}'
   
   # Wait 2s
   
   # Query: Tell me about Gabriella
   curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Tell me about Gabriella"}],...}'
   
   # Query: Full family
   curl ... -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Tell me about my family"}],...}'
   ```
   - Expected: Gabriella by NAME (not UUID), age=10, parent_of relationship, all 3 children visible
   - PASS/FAIL: ___
   - Database verify: `SELECT * FROM entity_name_conflicts WHERE status IN ('pending', 'resolved');` — should show resolved or empty
   - **CRITICAL:** If Gabriella is UUID or missing, test FAILS

4. **Scenario 3: System Metadata**
   - Input: "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"
   - Wait 2s
   - Query: "Tell me about my computer"
   - Expected: System facts stored OR gracefully missing (LLM confusion acceptable)
   - PASS/FAIL: ___

5. **Scenario 4: Sensitivity Gating + Age**
   - Input: "I was born on January 15, 1990"
   - Wait 1s
   - Query 1: "Tell me about me" (generic, no explicit birthday ask)
   - Expected: Birthday NOT returned (gate working)
   - Query 2: "How old am I"
   - Expected: Age returned (age=36, NOT 192)
   - Database verify: `SELECT * FROM entity_attributes WHERE attribute='age';` — should show age=36
   - PASS/FAIL: ___
   - **CRITICAL:** If age=192 is stored, validation didn't work

6. **Scenario 5: Transitive Relationships**
   - Input: "My friend Alice knows my sister Sarah"
   - Wait 2s
   - Query: "Who knows my family"
   - Expected: Alice and/or Sarah mentioned OR gracefully missing
   - PASS/FAIL: ___

7. **Summary & Report:**
   - All 5 scenarios: PASS or FAIL count
   - UUID leaks: None detected ✓ or describe leak
   - Age validation: Working ✓ or describe failure
   - Entity_attributes: Surfaced ✓ or missing
   - Database state: Consistent, no orphans ✓ or describe issues

8. **Update scratch** with completion entry (template below)

### Deliverable:

- **Comprehensive test report** (in scratch.md) — all 5 scenarios with results, database state, pass/fail count

### Success Criteria:

- All 5 scenarios PASS ✓
- No UUID leaks ✓
- Age validation correct (36 accepted, 192 rejected) ✓
- Entity_attributes surfaced ✓
- Sensitivity gating correct ✓
- Database consistent ✓

### Upon Completion:

**If all 5 scenarios PASS:**

Update scratch.md with:
```
## ✓ DONE: dprompt-37b (Full Pre-Prod Re-Validation) — 2026-05-10

**All 5 scenarios executed and verified:**

✓ Scenario 1 (Family Ingest + Query): Mars, Cyrus, Desmonde by name
✓ Scenario 2 (Gabriella Canary): Gabriella visible by name, age=10, all 3 children in family
✓ Scenario 3 (System Metadata): [passed / gracefully skipped due to LLM confusion]
✓ Scenario 4 (Sensitivity + Age): Age=36 calculated correctly, birthday gated, returned on explicit ask
✓ Scenario 5 (Transitive Relationships): [passed / gracefully handled]

**Validations confirmed:**
- No UUID leaks in any query response ✓
- Entity names used consistently ✓
- Age validation working (Person 0–150, non-Person unlimited) ✓
- Entity_attributes surfaced to /query ✓
- Sensitivity gating working ✓
- Database consistent, no orphans ✓

**System is production-ready. All edge cases fixed and validated end-to-end.**

Next: Deploy / proceed as directed.
```

**If any scenario FAILS:**

Update scratch.md with:
```
## ❌ FAILED: dprompt-37b (Pre-Prod Validation) — Scenario X failure

**Scenario:** [which one]
**Input:** [what was sent]
**Expected:** [what should happen]
**Actual:** [what happened]
**Database state:** [relevant queries]
**Root cause assessment:** [what's broken]

Awaiting direction on fix or investigation.
```

Then STOP. Do not attempt to fix. Wait for direction.

### CRITICAL NOTES:

- **All 5 must pass.** If even one fails, not production-ready.
- **UUID leak is a killer.** Zero tolerance. If any UUID appears in a query response, system fails.
- **Age validation is critical.** If age=192 gets stored, validation is broken.
- **Database state matters.** Don't just check responses; verify tables are consistent.
- **Gabriella is the canary.** If she's missing or appears as UUID, everything is broken.

### Motivation:

This is the final validation. All fixes are deployed. All edge cases should be gone. Run all 5 scenarios, document every result, confirm system is production-ready. If it all passes, FaultLine is shippable.

