# Test Sequencing — Unit + Integration + E2E (Required Before Production Deployment)

This document defines the test phases required before deploying any FaultLine changes to production. It exists because: dBug-016 revealed that unit tests pass but integration tests fail. We now require explicit multi-phase testing.

## Phase 1: Unit Tests (Local — Required)

**Environment:** Developer local machine (`/home/user/Documents/013-GIT/FaultLine-dev`)
**Tools:** pytest
**Command:**
```bash
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
        --ignore=tests/model_inference --ignore=tests/preprocessing -v
```

**Success criteria:**
- ≥141 tests passed
- 0 new failures (pre-existing failures acceptable if documented)
- 0 regressions vs previous release

**What it catches:**
- API endpoint logic (ingest, query, retract, extract)
- Database schema + migrations
- Entity resolution + UUID handling
- Fact classification + staging
- Cache logic
- Individual function behavior

**What it misses:**
- OpenWebUI integration (no OpenWebUI container in test)
- Filter inlet/outlet behavior (mocked, not real)
- LLM call integration (mocked responses)
- End-to-end flow (user input → facts → context injection)
- Docker environment issues

## Phase 2: Integration Tests (Pre-Prod — Required Before Deployment)

**Environment:** Pre-prod instance (docker-host, ${OPENWEBUI_DOMAIN})
**Setup:** Deploy code to pre-prod container (docker build + restart)
**Tools:** curl, docker logs, psql CLI

### 2A: Backend API Tests (Direct HTTP)

**Test:** Medical fact ingestion
```bash
curl -s -X POST http://${BACKEND_IP}:8001/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "I hurt my back gardening",
    "user_id": "integration-test-001",
    "source": "test",
    "edges": [
      {
        "subject": "user",
        "object": "back pain",
        "rel_type": "has_medical_condition",
        "subject_type": "Person",
        "object_type": "Concept"
      }
    ]
  }' | jq .
```

**Verify:**
- ✓ Response status: 200 OK
- ✓ "staged": 1 or "committed": 1 (fact persisted)
- ✓ "status": "valid"

**Test:** Query returns staged facts
```bash
curl -s -X POST http://${BACKEND_IP}:8001/query \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "integration-test-001", "text": "medical"}' | jq '.facts'
```

**Verify:**
- ✓ Medical facts present (has_medical_condition, etc.)
- ✓ "fact_state": "staged"
- ✓ Confidence: 0.8 (as pre-seeded)

### 2B: Filter Integration Tests (OpenWebUI → Backend)

**CRITICAL:** This is where dBug-016 was missed.

**Test 1:** Filter inlet produces valid request body
```bash
# Trigger a question-form message through OpenWebUI UI
# Send: "What did I do to my back?"
# Observe: No 400 errors in open-webui logs

ssh docker-host -x "sudo docker logs open-webui --tail 50 2>&1" | grep -i error
# Expected: No errors related to NoneType or process_chat
```

**Verify:**
- ✓ No "NoneType" errors in OpenWebUI logs
- ✓ No "400 Bad Request" responses
- ✓ Filter system messages injected (check logs for "INJECTED")

**Test 2:** Filter system message structure is valid JSON
```bash
# Check that injected facts are properly formatted
ssh docker-host -x "sudo docker logs faultline-filter --tail 100 2>&1 | grep 'INJECTED'"
# Expected: Shows "[FaultLine Filter] INJECTED total_messages=N" (no errors)
```

**Verify:**
- ✓ System messages contain valid JSON
- ✓ No truncated or malformed dict structures
- ✓ All string fields are strings (not None)

**Test 3:** LLM receives request without errors
```bash
# Check that rewrite_to_triples succeeds (LLM call goes through)
ssh docker-host -x "sudo docker logs faultline --tail 100 2>&1 | grep -i 'rewrite_to_triples'"
# Expected: Shows successful LLM calls, NOT "HTTP error: 400"
```

**Verify:**
- ✓ LLM calls return 200 OK
- ✓ No "NoneType" errors in response
- ✓ Triples extracted (or gracefully handled as empty)

**Test 4:** End-to-end: Question → Facts → Context Injection
```bash
# Send a medical question through OpenWebUI UI
# Expected: LLM response includes medical context (personalized, not generic)
```

**Verify:**
- ✓ No OpenWebUI 400 errors
- ✓ LLM generates response (not timeout/error)
- ✓ Response mentions medical facts (back, pain, etc.)
- ✓ No generic response ("I'm sorry, I don't have context about your medical...")

### 2C: Database State Verification

```bash
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test \
  -c \"SELECT rel_type, COUNT(*) FROM staged_facts \
      WHERE user_id LIKE 'integration-test%' \
      GROUP BY rel_type ORDER BY rel_type;\" "
```

**Verify:**
- ✓ Medical facts present in staged_facts
- ✓ Correct rel_types (has_medical_condition, has_symptom, etc.)
- ✓ fact_class='B', confidence=0.8 (as pre-seeded)

## Phase 3: Regression Tests (Pre-Prod — Verify No Breakage)

**Test:** Existing functionality still works

**Test 1:** Identity facts still work
```bash
# "My name is ${USER}"
# Expected: pref_name fact created
```

**Test 2:** Family relationships still work
```bash
# "I have 2 kids"
# Expected: child_of facts created
```

**Test 3:** Knowledge graph queries still work
```bash
curl -s -X POST http://${BACKEND_IP}:8001/query \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "FaultLine WGM Test 1.0", "text": "family"}' | jq '.facts | length'
# Expected: ≥10 facts returned (existing knowledge graph intact)
```

**Test 4:** No new errors in logs
```bash
ssh docker-host -x "sudo docker logs faultline --tail 200 2>&1 | grep -i error | head -10"
ssh docker-host -x "sudo docker logs open-webui --tail 200 2>&1 | grep -i error | head -10"
# Expected: No NEW error patterns (pre-existing ones documented)
```

## Deployment Approval Checklist

Before deploying to production, **ALL** of the following must pass:

### Unit Tests (Phase 1)
- [ ] ≥141 tests passed locally
- [ ] 0 new failures
- [ ] No regressions (compare vs previous release)

### Integration Tests (Phase 2A — Backend API)
- [ ] Medical fact ingestion works (HTTP 200, facts staged)
- [ ] /query returns staged medical facts
- [ ] Medical facts have correct metadata (fact_class='B', confidence=0.8)

### Integration Tests (Phase 2B — Filter)
- [ ] Filter inlet produces valid request body (no NoneType errors)
- [ ] System message injection works (valid JSON)
- [ ] LLM receives request without errors (HTTP 200, not 400)
- [ ] End-to-end medical extraction works (LLM response includes context)

### Integration Tests (Phase 2C — Database)
- [ ] Medical facts in staged_facts with correct rel_types
- [ ] fact_class='B' (behavioral, staged)
- [ ] confidence=0.8 (high, pre-seeded)

### Regression Tests (Phase 3)
- [ ] Identity facts (pref_name) still work
- [ ] Family relationships (child_of, parent_of) still work
- [ ] Knowledge graph queries return existing facts
- [ ] No new errors in logs (vs baseline)

---

## Who Runs Each Phase

**Phase 1 (Unit Tests):** Developer (before git commit)
- Responsibility: Run tests locally, fix any failures
- Gate: No commits pushed without passing Phase 1

**Phase 2 & 3 (Integration):** Deepseek or authorized tester (after git commit, before production)
- Responsibility: Deploy to pre-prod, run full test sequence
- Gate: No production deployment without passing Phase 2 & 3
- Report: Document results in scratch.md completion note

**Phase 2 & 3 Validation:** Claude (review results, verify quality)
- Responsibility: Check logs, verify all test criteria met
- Gate: Approve or block production deployment

---

## When Phase 2 Would Have Caught dBug-016

**Test:** Filter inlet with OpenWebUI (Phase 2B, Test 1)
- Trigger: "What did I do to my back?"
- Expected: No errors
- Actual: OpenWebUI logs show "NoneType object has no attribute 'startswith'"
- **Result:** Integration test FAILS → Deployment blocked → dBug-016 caught before production

**Why unit tests missed it:**
- Unit tests mock OpenWebUI (no actual container)
- Filter inlet tested in isolation (no real OpenWebUI request structure)
- Integration with real OpenWebUI's process_chat not tested

## Maintenance

This document is the source of truth for deployment test requirements. Update it when:
- New integration issues discovered (add new test)
- Bug patterns emerge (add specific regression tests)
- Architecture changes (update test scope)
- Deploy process changes (update who/when/how)

Last updated: 2026-05-12 (after dBug-016 discovery)

