# dprompt-37 — Comprehensive Pre-Prod Validation: All 5 Scenarios Full-Pass

## Purpose

Re-run all 5 end-to-end scenarios from dprompt-34b against the now-fixed pre-production instance to validate complete system readiness: ingest → collision detection → re-embedder resolution → query, with all edge cases fixed (UUID leak, age validation, entity_attributes surfacing).

## Test Scenarios (All 5)

### Scenario 1: Family Ingest + Query
Input: "We have two kids: Cyrus and Desmonde, and a spouse Mars"
Expected: Query "What is my family" returns Mars, Cyrus, Desmonde by NAME

### Scenario 2: Gabriella Collision (Canary)
Input: "I go by Gabby" then "We have a third daughter Gabriella who is 10 and goes by Gabby"
Expected: No UUID leak, Gabriella visible by name, age=10 correct, all 3 children in family query

### Scenario 3: System Metadata
Input: "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"
Expected: System facts stored (or gracefully skipped if LLM confusion), no crashes

### Scenario 4: Sensitivity Gating + Age
Input: "I was born on January 15, 1990"
Expected: Age calculated correctly (36, not 192), generic query doesn't leak birthday, explicit query "How old am I" returns age

### Scenario 5: Transitive Relationships
Input: "My friend Alice knows my sister Sarah"
Expected: Entities stored, relationships captured if LLM extracts them

## Validation Checklist

**All 5 scenarios must:**
- [ ] Execute without error via curl API calls
- [ ] Return expected results in /query responses
- [ ] Use entity NAMES (not UUIDs) in all responses
- [ ] Handle collisions correctly (if any)
- [ ] Validate ages properly (Person 0–150, non-Person unlimited)
- [ ] Surface entity_attributes when relevant
- [ ] Log implausible values, don't reject silently

**Database verification (via SSH):**
- [ ] Facts table has expected entries
- [ ] entity_name_conflicts empty or resolved (no pending)
- [ ] entity_attributes has ages/metadata
- [ ] No UUID leaks in entity_aliases preferred names

## Success Criteria

- All 5 scenarios PASS ✓
- No UUID leaks in any query response ✓
- Age validation working (36 accepted, 192 rejected) ✓
- Entity_attributes surfaced (age returned on query) ✓
- Sensitivity gating working (birthday gated unless explicit) ✓
- System stable (no crashes, errors, timeouts) ✓

## Failure Handling

If any scenario fails:
1. Document exact input, expected output, actual output
2. Database state (SSH queries)
3. Error messages or timeout logs
4. Identify which component failed (Filter, ingest, query, re-embedder)
5. Stop and report findings in scratch

