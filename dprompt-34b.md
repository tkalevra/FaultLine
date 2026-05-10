# dprompt-34b — Pre-Production Validation: Live OpenWebUI API Testing [PROMPT]

## #deepseek NEXT: dprompt-34b — Pre-Prod Live Testing (OpenWebUI API) — 2026-05-10

### Task:

Test FaultLine against the **live pre-production OpenWebUI instance** via API calls with bearer token. Execute 5 end-to-end scenarios: ingest → collision detection → re-embedder resolution → query. Validate that natural language queries ("What's my family") return all entities with preferred names (no UUIDs, no missing entities).

### Context:

Database on truenas has been wiped (schema dropped and recreated). Pre-prod instance is running at https://hairbrush.helpdeskpro.ca/?models=faultline-wgm-test-10. This is the **true end-to-end validation**: real Filter LLM extraction, real WGM gate, real ingest pipeline, real collision detection, real re-embedder resolution, real query results.

Unit tests said production-ready. Gabriella bug proved they were wrong. Live testing caught it. This validation confirms the fix works in production: collisions detected, LLM-resolved, all entities visible in queries with their real names.

### Constraints (CRITICAL):

- **Wrong: Test against localhost or mock endpoints**
- **Right: Test against live OpenWebUI instance https://hairbrush.helpdeskpro.ca/?models=faultline-wgm-test-10**
- **MUST: Bearer token ONLY: `sk-addb2220bf534bfaa8f78d96e6991989`. Do NOT switch models.**
- **MUST: Model LOCKED to `faultline-wgm-test-10`. Do NOT test other models.**
- **MUST: Use curl for API calls. Example: `curl -X POST -H "Authorization: Bearer sk-..." https://hairbrush.helpdeskpro.ca/api/chat/completions ...`**
- **MUST: Execute 5 scenarios in order. Do NOT skip steps.**
- **MUST: Collect responses + database state. Document in scratch upon completion.**
- **MUST: If any scenario fails or returns wrong results, STOP and document findings (do NOT fix). Wait for direction.**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-34.md spec carefully (all sections, especially test scenarios)
2. Verify database wiped: `ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT COUNT(*) FROM facts;'"` — should return 0
3. **Scenario 1: Family Ingest + Query**
   - Send via curl: "We have two kids: Cyrus and Desmonde, and a spouse Mars"
   - Wait 2s for ingest
   - Query: "What is my family"
   - **Expected:** Response mentions Mars, Cyrus, Desmonde by NAME (not UUID)
   - **Save:** Filter response, query result
4. **Scenario 2: Gabriella Name Collision (The Canary)**
   - Send: "I go by Gabby"
   - Wait 1s
   - Send: "We have a third daughter Gabriella who is 10 and goes by Gabby"
   - Wait 1s
   - **Verify via SSH:** `ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT COUNT(*) FROM entity_name_conflicts WHERE status=\"pending\";'"` — should return 1 or 2 (collisions detected)
   - Wait 5s for re-embedder cycle to resolve
   - **Verify via SSH:** Same query with `status=\"resolved\"` — should return non-zero (conflict resolved)
   - Query: "Tell me about Gabriella"
   - **Expected:** Response mentions Gabriella by NAME. Parent relationship returned. No "missing" or "UUID" references.
   - **Save:** All SSH query outputs, Filter response, query result
5. **Scenario 3: System Metadata**
   - Send: "My laptop is named Mars, IP is 192.168.1.100, OS is Ubuntu 22.04"
   - Wait 2s
   - Query: "Tell me about my computer"
   - **Expected:** Response mentions system facts (name, IP, OS)
   - **Save:** Filter response, query result
6. **Scenario 4: Sensitivity Gating**
   - Send: "I was born on January 15, 1990"
   - Wait 1s
   - Query: "Tell me about me" (no explicit birthday ask)
   - **Expected:** Response does NOT include birth date (sensitivity gate)
   - Query: "How old am I"
   - **Expected:** Response includes birth date or calculated age (explicit ask overrides gate)
   - **Save:** Both query results
7. **Scenario 5: Transitive Relationships**
   - Send: "My friend Alice knows my sister Sarah"
   - Wait 2s
   - Query: "Who knows my family"
   - **Expected:** Response mentions Alice as knowing Sarah
   - **Save:** Filter response, query result
8. Collect all findings: Filter responses, query results, SSH database verifications
9. Document in scratch.md using template below

### Deliverable:

- **Scenario execution report** (in scratch.md) — all 5 scenarios with:
  - Input message (what was sent to Filter)
  - Filter response (what LLM extracted/acknowledged)
  - Query result (what /query returned)
  - Database verification (SSH queries: facts count, collision table, etc.)
  - **Pass/Fail assessment:** Did results match expectations?

### Files to Modify:

- **Update:** `scratch.md` (completion entry from template below)
- **NO code changes. Tests only.**

### Success Criteria:

- All 5 scenarios execute without error ✓
- OpenWebUI Filter responds with acknowledgment ✓
- Gabriella collision detected in entity_name_conflicts ✓
- Gabriella collision resolved (status="resolved" in table) ✓
- Query results use entity NAMES, not UUIDs ✓
- No missing entities ("Gabriella missing" = FAIL) ✓
- Sensitivity facts gated correctly (birthday not leaked) ✓
- All findings documented in scratch ✓

### Upon Completion:

**If all 5 scenarios pass:**

Update scratch.md with this entry (COPY EXACTLY):
```
## ✓ DONE: dprompt-34b (Pre-Prod Live Testing — OpenWebUI API) — 2026-05-10

**Test environment:** hairbrush.helpdeskpro.ca, model=faultline-wgm-test-10, bearer token validated
**Database:** Clean, wiped before testing

**Scenario 1 (Family Ingest + Query):** ✓ PASS
- Ingest: Mars, Cyrus, Desmonde
- Query "What is my family": returned all 3 by name ✓

**Scenario 2 (Gabriella Collision — The Canary):** ✓ PASS
- Ingest: User "Gabby" + Gabriella "Gabby" (collision)
- entity_name_conflicts: collision detected pending ✓
- Re-embedder cycle: collision resolved, status=resolved ✓
- Query "Tell me about Gabriella": returned Gabriella by name + parent_of fact ✓

**Scenario 3 (System Metadata):** ✓ PASS
- Ingest: system facts (name, IP, OS)
- Query "Tell me about my computer": returned system facts ✓

**Scenario 4 (Sensitivity Gating):** ✓ PASS
- Birthday fact ingested
- Query without explicit ask: birthday NOT returned (gate working) ✓
- Query "How old am I": birthday/age returned (explicit ask override) ✓

**Scenario 5 (Transitive Relationships):** ✓ PASS
- Ingest: Alice knows Sarah
- Query "Who knows my family": Alice returned ✓

**Overall:** System is production-ready. Integration pipeline validated end-to-end. All entities visible with correct names. Collision detection + LLM resolution working. Sensitivity gating working. Query results accurate.

Next: [awaiting direction for deployment]
```

**If any scenario fails:**

Update scratch.md with:
```
## ❌ FAILED: dprompt-34b (Pre-Prod Live Testing) — Scenario X failure

**Scenario:** [which one]
**Expected:** [what should happen]
**Actual:** [what happened]
**Evidence:** [Filter response / query result / database state]

Awaiting direction on fix or further investigation.
```

Then STOP. Do not attempt to fix. Wait for direction.

### CRITICAL NOTES:

- **Live instance is non-negotiable.** Test against hairbrush.helpdeskpro.ca, not localhost.
- **Bearer token locked.** Do NOT switch auth methods or models.
- **Gabriella is the canary.** If she's missing from query results, conflict resolution is broken.
- **All 5 must pass.** If even one fails, production readiness is questionable. Document and wait for direction.
- **Entity names only.** Query results should mention "Gabriella", "Mars", "Cyrus" — not UUIDs, not fallback aliases.

### Motivation:

This is the truth test. Unit tests said production-ready. Live system proves otherwise (or confirms the fix works). Gabriella bug should be invisible: collision detected by ingest, resolved by re-embedder LLM, all entities visible with correct names in query results. That's what production-ready means.

