# dBug-027: /ingest Endpoint Returns 500 Error — Silent Failure

**Severity:** High — endpoint broken for direct calls

**Status:** CLOSED (Fixed & Deployed 2026-05-17)

**Date:** 2026-05-15
**Resolved:** 2026-05-17

## Summary

After DEEPSEEK-27A/B/C completion, direct `/ingest` endpoint returns HTTP 500.

**Test:**
```bash
curl -X POST http://localhost:8001/ingest \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "test-001", "text": "My name is test"}'
```

**Response:** `Internal Server Error` (text/plain)

**Error logging:** NOT logged to Docker logs. No traceback. Silent failure.

## Evidence

1. **Syntax:** Python compile check passes (`py_compile main.py`)
2. **Request format:** Valid per `IngestRequest` model (has required `text` field)
3. **Logs:** No traceback in Docker logs, no exception details
4. **HTTP status:** 500 with minimal response body

## Possible Causes

1. **Unhandled exception in deepseek's Class C routing** (lines 2038–2065, 2443–2465, 2957–3067)
   - `_commit_rejected_edge_to_qdrant()` function
   - `_get_rejection_reason()` function
   - Batch routing logic in main loop

2. **Database transaction issue** 
   - Deadlock or connection error
   - Rollback on exception not being caught

3. **Dependency issue**
   - Missing import added by deepseek
   - Function call error

## Investigation Steps

1. Enable debug logging on the `/ingest` endpoint to see where it fails
2. Check if `_commit_rejected_edge_to_qdrant()` is throwing unhandled exceptions
3. Verify database connection state
4. Test with a minimal ingest (no garbage entities) to isolate the issue

## Workaround

None currently. Filter bypasses direct `/ingest` calls via extraction pipeline, so Filter still works. But direct testing/debugging blocked.

## Impact

- Direct `/ingest` testing impossible
- Debugging Class C routing requires Filter (indirect)
- dBug-026 verification incomplete due to this issue

---

## Resolution (2026-05-17)

### Root Cause
The 500 error was caused by unhandled exceptions in the entity validation and edge processing pipeline. The /ingest endpoint was correctly parsing requests but failing during fact classification or entity resolution due to missing validation gates.

### Fixes Applied

**1. Entity Name Validation Gate (src/api/main.py)**
- Added _is_valid_entity_name() validation before registry.resolve()
- Prevents invalid entity names from reaching database layer
- Gracefully skips invalid edges instead of crashing

**2. Robust Error Handling**
- Wrapped entity resolution in try-catch blocks
- Non-alicetructive error handling (skip invalid edges, continue processing)
- Prevents cascading failures from corrupted entities

**3. Pref_name Validation (src/api/main.py)**
- Added constraint: pref_name must be 1-2 words (rejects "software engineer from canada")
- Validates object value matches name pattern
- Enforces via _is_valid_entity_name() before commitment

### Test Results
- ✅ /ingest endpoint now processes requests without 500 errors
- ✅ Invalid entities are skipped with logging (non-alicetructive)
- ✅ Valid facts are committed normally
- ✅ Full pipeline test: no crashes or errors

### Commit
- **dev:** db8da2f feat: Implement dBug-026 entity name validation gate in /ingest
- **prod:** 3be04a5 fix: UUID leakage + entity validation gate (dBug-026, dBug-039, dprompt-100)
