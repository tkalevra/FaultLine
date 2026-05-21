# dBug-013: User Medical Context Not Ingested or Retrieved

## alicecription

User provided feedback in OpenWebUI about a slipped disk injury sustained while gardening on the weekend. This is concrete, real user context that should:

1. **Be ingested** into the knowledge graph with proper facts about the injury (what, where, when, cause)
2. **Build ontology/hierarchy** — create medical condition entities, link to body parts, temporal context
3. **Be retrievable** — subsequent queries about health/wellness should surface this context
4. **Be stored in RAG** — at minimum as vector context for semantic memory recall

**What actually happened:** Neither ingest nor RAG storage occurred. System generated generic medical advice without acknowledging the specific injury context provided.

## Reproduction

**Setup:**
```bash
# Pre-prod is running with FaultLine v1.0.7
ssh docker-host -x "curl -s http://localhost:8001/health | jq ."
# Returns: ok
```

**Step 1: Test ingest with unknown rel_types**
```bash
curl -X POST http://localhost:8001/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "I slipped a disk in my back while gardening on the weekend",
    "user_id": "FaultLine WGM Test 1.0",
    "source": "test",
    "edges": [
      {
        "subject": "user",
        "object": "slipped disk",
        "rel_type": "has_medical_condition",
        "subject_type": "Person",
        "object_type": "MedicalCondition"
      }
    ]
  }'
```

**Result:**
```json
{
  "status": "valid",
  "committed": 0,
  "staged": 1,
  "facts": [
    {
      "rel_type": "has_medical_condition",
      "fact_class": "C",
      "status": "unknown"
    }
  ]
}
```

**Issue:** Fact staged as Class C (ephemeral), not committed to facts table. Rel_type unknown. No hierarchy created.

---

**Step 2: Test ingest with known Class A rel_type (born_on)**
```bash
curl -X POST http://localhost:8001/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "I was born on May 12",
    "user_id": "FaultLine WGM Test 1.0",
    "source": "test",
    "edges": [
      {
        "subject": "user",
        "object": "May 12",
        "rel_type": "born_on",
        "subject_type": "Person",
        "object_type": "Date"
      }
    ]
  }'
```

**Result:**
```json
{
  "status": "valid",
  "committed": 0,
  "staged": 0,
  "facts": []
}
```

**Issue:** Zero facts. Edge silently dropped. No error, no fallback, no staging.

---

**Step 3: Database verification**
```bash
# Check facts table for user
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test \
  -c \"SELECT COUNT(*) FROM facts WHERE user_id = 'FaultLine WGM Test 1.0';\""
```

**Result:** 0 rows (no facts for this user)

---

**Step 4: Check entity_aliases (entities created but not linked)**
```bash
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test \
  -c \"SELECT entity_id, user_id, alias, is_preferred FROM entity_aliases \
       WHERE user_id = 'FaultLine WGM Test 1.0';\""
```

**Result:**
```
entity_id                            | user_id                | alias         | is_preferred
cf5dd104-51f7-5dd2-98d7-4852e5528647 | FaultLine WGM Test 1.0 | slipped disk  | t
cd723f03-127b-5477-bdb6-db0493a9a9ea | FaultLine WGM Test 1.0 | back pain     | t
```

**Issue:** Entities were created and aliases registered, but NO facts link them together.

---

**Step 5: Check staged_facts (only unknown rel_types staged)**
```bash
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test \
  -c \"SELECT * FROM staged_facts WHERE user_id = 'FaultLine WGM Test 1.0';\""
```

**Result:**
```
user_id                | subject_id                           | rel_type              | fact_class | confirmed_count
FaultLine WGM Test 1.0 | dc97192a-3fc6-585d-bc8e-47a3489373a3 | has_medical_condition | C          | 0
FaultLine WGM Test 1.0 | dc97192a-3fc6-585d-bc8e-47a3489373a3 | has_symptom           | C          | 0
```

**Issue:** Only unknown rel_types (Class C) are staged. Known rel_types vanish with no trace.

## Impact

**Severity: CRITICAL (data loss path)**

- **Knowledge graph is incomplete:** Real user context (medical, personal) is not persisted
- **Regression risk:** This affects all users, not just test cases
- **User experience:** System appears to ignore context, generates irrelevant advice
- **RAG failure:** Vector store has no semantic context for future queries
- **Non-recoverable:** Facts are dropped silently; no error to signal a problem

**Scope:**
- All ingest calls with known rel_types (Class A/B) from external users
- Affects identity facts (pref_name, born_on, etc.) and behavioral facts (works_for, lives_at, etc.)
- Only unknown rel_types make it through (Class C) → system appears to work but silently loses data

## Root Cause Analysis (TBD — Deepseek Investigation)

**Hypotheses to verify:**

1. **Entity resolution failure** — user_id string not matching fact lookup
2. **Validation gate blocking** — WGMValidationGate rejecting facts without error
3. **Fact classification logic** — all facts incorrectly classified as unknown/Class C
4. **Database constraint violation** — unique constraint causing silent INSERT failure
5. **Filter-level filtering** — OpenWebUI filter stripping edges before /ingest
6. **LLM extraction** — Filter's LLM not extracting edges from medical context

**Investigation required:**
- Trace /ingest endpoint for the born_on edge (should be committed, not dropped)
- Check WGMValidationGate.validate() for rejection patterns
- Verify fact_class assignment logic (why is born_on becoming unknown?)
- Check logs for SQL errors or constraint violations
- Verify Filter LLM prompt extracts medical-related rel_types

## Proposed Fix (TBD)

After deepseek's analysis, fix must address:
1. Known rel_types must be committed to facts (not staged or dropped)
2. Unknown rel_types must be staged to staged_facts with clear status
3. User context must reach RAG (either via facts or /store_context fallback)
4. No silent data loss — errors must be logged or returned

## Validation Plan

After fix:
1. `pytest tests/api/test_ingest.py -v` — all ingest tests pass
2. Reproduce steps 1–5 above — facts table now has entries
3. Query /query for medical context — returns injected facts
4. Check Qdrant collections — medical facts appear in vector search
5. Verify non-regression — existing tests (114+) still pass

## Related Issues

- dBug-012: Bidirectional relationship completeness (separate issue)
- Filter may not be calling /ingest (verify in next phase)
