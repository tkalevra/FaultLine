# dBug-report-010: Novel Rel_Types Not Staged as Class C

**Date Reported:** 2026-05-15  
**Severity:** P1 (Feature blocking)  
**Status:** Open  
**Version:** v1.0.7

## Summary

Novel rel_types (status="unknown" from WGM gate) are recorded in `ontology_evaluations` but **never staged to `staged_facts` as Class C**. They're silently dropped from the ingest pipeline despite being classified as Class C.

Health facts example: `(user, has_injury, back, 0.4)` returns status="unknown" but is never staged → never available in `/query`.

## Root Cause

**File:** `src/api/main.py`, line 2378

```python
if status in ("valid", "conflict"):
    rows.append((
        req.user_id, fact_subject, canonical_object,
        edge.rel_type, req.source, is_pref,
        fact_class, edge_confidence, is_engine_generated
    ))
```

Facts with `status="unknown"` (novel rel_types from WGM gate) are **excluded from rows**. They never enter the staging pipeline → never reach `staged_facts`.

## Expected Flow (Current — BROKEN)

```
Novel rel_type ("has_injury")
├─▶ WGM gate → status="unknown"
├─▶ Record in ontology_evaluations ✓
└─▶ NOT added to rows (line 2378 condition fails)
      └─▶ Never staged to staged_facts ✗
            └─▶ Never available in /query ✗
```

## Expected Flow (CORRECT)

```
Novel rel_type ("has_injury")
├─▶ WGM gate → status="unknown"
├─▶ Record in ontology_evaluations ✓
├─▶ Add to rows (line 2378 includes "unknown")
│     └─▶ Classified as Class C (line 141: engine_generated=True)
│           └─▶ Staged to staged_facts with fact_class='C', confidence=0.4 ✓
│                 └─▶ Re-embedder evaluates: approve/map/reject
│                       └─▶ Available in /query immediately (staged facts union) ✓
```

## Fix

**Line 2378:** Add `"unknown"` to the status check:

```python
if status in ("valid", "conflict", "unknown"):
    rows.append((
        req.user_id, fact_subject, canonical_object,
        edge.rel_type, req.source, is_pref,
        fact_class, edge_confidence, is_engine_generated
    ))
```

**Lines changed:** 1  
**Complexity:** Trivial  
**Risk:** None (unknown rel_types are already designed to be Class C)

## Validation

After fix, test with:
```bash
curl -X POST http://192.168.40.10:8001/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "I pulled my back",
    "user_id": "test-health",
    "edges": [
      {"subject": "user", "rel_type": "has_injury", "object": "back", "confidence": 0.4}
    ],
    "source": "test"
  }'

# Expected response:
{
  "status": "valid",
  "staged": 1  # THIS SHOULD NOT BE 0
}

# Verify in DB:
psql -c "SELECT rel_type, fact_class FROM staged_facts WHERE rel_type='has_injury'"
# Expected: has_injury | C
```

## Notes

- Novel rel_type pipeline was designed correctly (WGM returns "unknown", ingest routes to Class C)
- The bug is in the routing logic — the condition was never updated to include "unknown"
- dprompt-69 extraction prompt change works correctly, but facts are silently dropped due to this routing bug
- This explains why health facts appear "unknown" in API response but don't persist

---

**Root Cause:** Line 2378 condition incomplete  
**Type:** Routing logic error  
**Priority:** P1 (blocks dprompt-69 functionality)  
**Assigned to:** claude (immediate fix)
