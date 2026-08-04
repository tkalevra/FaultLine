# FaultLine Transparent Test Skills

**Goal:** Test the full pipeline transparently, stop on failure, investigate, and propose fixes. All constraint-respecting, metadata-driven, no hardcoding.

## Skills

### 1. **test-pipeline-transparent**
Complete end-to-end test: Clear DB → Ingest family → Validate all tables → Check response quality.

Shows actual curl commands and SQL queries. Stops with STOP messages on first error.

**Use:**
```bash
/skill test-pipeline-transparent
```

**What it does:**
- Clears database (fresh state)
- Sends family ingest via curl (transparent, not hidden)
- Validates 6 entities created (5 Person + 1 Animal)
- Validates relationships: spouse, parent_of, child_of, has_pet
- Validates scalar attributes: ages stored in entity_attributes (not facts)
- Checks response quality (plain English, no creepy messages)

**Expected output on PASS:**
```
✅ PIPELINE TEST PASSED
- Database cleared and populated correctly
- 6 entities created (5 Person + 1 Animal)
- Relationships stored: spouse (1), parent_of (3), child_of (3), has_pet (1)
- Scalar attributes (ages) in entity_attributes table
- LLM response is plain English, no creepy messages
```

**On FAIL:**
Stops at first failure with STOP message and exact evidence. Example:
```
STOP: Database truncation failed. Check Docker/Postgres connectivity.
```

---

### 2. **test-faultline-corrections**
Tests user corrections (Class A override) and fact updates.

**Use (after test-pipeline-transparent):**
```bash
/skill test-faultline-corrections
```

**What it tests:**
- Age correction: Des 12 → 14 (validates entity_attributes updated)
- Name preference: chris → christopher (validates entity_aliases updated)
- Confirms corrections are Class A (confidence 1.0)
- No data loss

---

### 3. **test-faultline-retractions**
Tests fact removal via retraction mechanism.

**Use (after test-faultline-corrections):**
```bash
/skill test-faultline-retractions
```

**What it tests:**
- Retraction signal ("forget pets") processed
- has_pet facts marked superseded_at (soft-delete, audit trail preserved)
- Qdrant sync flag set for cleanup
- Active query excludes superseded facts

---

### 4. **test-faultline-name-changes**
Tests alias registration and preferred name updates.

**Use (after test-faultline-retractions):**
```bash
/skill test-faultline-name-changes
```

**What it tests:**
- New alias registered (Cyrus → Cy)
- All aliases are display names (no UUIDs)
- Entity count consistent across tables
- No orphaned relationships

---

### 5. **investigate-faultline-failure**
When any test fails, run this to see:
- Backend API logs (last 50 lines)
- OpenWebUI Filter logs
- Database state (entities, aliases, facts, staged facts, attributes)
- CLAUDE.md constraint checks

**Use (when test fails):**
```bash
/skill investigate-faultline-failure
```

**Checks 10 hard constraints from CLAUDE.md:**
1. No UUID in entity_aliases
2. Validation metadata-driven via rel_types
3. All entities have display names
4. Scalar objects are STRING, not UUID
5. Filter injects display names, not UUIDs
6. All rel_type logic from DB, not hardcoded
7. Entity ID normalized to 'user' anchor
8. Three-dimensional classification model
9. Graph and hierarchy traversal separate
10. Deduplication uses UUIDs, not display names

**Output:** Identifies which constraint is violated.

---

### 6. **propose-faultline-fix**
Suggests fixes that respect CLAUDE.md. Takes investigation findings and proposes generic, metadata-driven solutions.

**Use (after investigating failure):**
```bash
/skill propose-faultline-fix
```

**Fix patterns provided:**
- Entity alias registration
- Scalar routing (age as STRING not UUID)
- UUID leakage in Filter
- Metadata-driven validation
- Confidence classification
- Display name lookup fallback

**Every fix is:**
- ✅ GENERIC (works for any entity type, domain, rel_type)
- ✅ NON-BRITTLE (no hardcoding, metadata-driven)
- ✅ CONSTRAINT-RESPECTING (verifies CLAUDE.md)

---

## Workflow

### Happy Path (All Tests Pass)
```bash
/skill test-pipeline-transparent
# ✅ All 6 steps pass

/skill test-faultline-corrections
# ✅ All corrections apply

/skill test-faultline-retractions
# ✅ All retractions soft-delete

/skill test-faultline-name-changes
# ✅ All names registered correctly

# RESULT: Pipeline is working correctly
```

### Failure Path (Stop, Investigate, Fix)
```bash
/skill test-pipeline-transparent
# ❌ STOP: Expected relationships not found. Check ingest validation pipeline.

/skill investigate-faultline-failure
# Shows backend logs, filter logs, DB state, constraint violations
# Finds: Constraint #2 violated (hardcoded rel_type check in code)

/skill propose-faultline-fix
# Shows: Pattern "Metadata-Driven Validation"
# Explains: Replace hardcoded rel_type checks with metadata queries
# Evidence: grep -r "rel_type ==" src/ returns matches (should be 0)

# YOU implement the fix
# Then re-run: /skill test-pipeline-transparent
```

---

## Key Principles

### Transparent = You See Everything
- Actual curl commands printed (not hidden)
- SQL queries visible (not abstracted)
- Test failures show exact error (not generic "test failed")
- Logs from backend/filter included (not hidden)

### Constraint-Respecting = CLAUDE.md Enforced
- No hardcoded rel_types (all from rel_types table)
- No hardcoded entity logic (all metadata-driven)
- UUID constraints enforced (display names only)
- Scope proper (memory pipeline, not family-specific)

### Generic = Works for Any Domain
- Test skill works for family, work, health, hobbies, etc.
- Fix suggestions work for any rel_type (not family-specific)
- All validation rules from metadata, not code

---

## Chainable or Independent?

### Chainable (default)
Run in order for full pipeline test:
1. test-pipeline-transparent
2. test-faultline-corrections
3. test-faultline-retractions
4. test-faultline-name-changes

### Independent
You can run any skill by itself if you already know what you're testing:
```bash
/skill test-faultline-corrections  # test just corrections
/skill investigate-faultline-failure  # diagnose without testing first
/skill propose-faultline-fix  # just see fix patterns
```

---

## Database Reset Between Tests

If you want to start fresh:
```bash
ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c \
  \"TRUNCATE entities, entity_aliases, facts, staged_facts, entity_attributes, entity_name_conflicts CASCADE;\""
```

Then run test-pipeline-transparent again.

---

## Next Steps

1. **First time:** Run the full chain in order (6 skills in sequence)
2. **If all pass:** Pipeline is working ✅
3. **If any fail:** Run investigate-failure → propose-fix → implement → re-run that test
4. **When confident:** Test with REAL data (not just family example)

---

## Philosophy

> **"Test transparently, fail loudly, fix generically."**

- Tests show you actual curl/SQL (not hidden)
- Failures stop immediately with exact evidence (not silent)
- Fixes are metadata-driven and work for any domain (not hardcoded)
- CLAUDE.md constraints are enforced automatically
