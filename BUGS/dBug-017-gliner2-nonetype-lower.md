# dBug-017: GLiNER2 NoneType Crash on Entity Extraction

**STATUS: ROOT CAUSE IDENTIFIED**

## alicecription

FaultLine's `/extract` endpoint (GLiNER2 entity extraction) crashes with `'NoneType' object has no attribute 'lower'` when processing queries like "what'd I do to my back?". The crash prevents entity creation, causing the extraction pipeline to return null subjects/objects, and blocking all downstream fact extraction.

**User action:** "what'd I do to my back?"
**Expected:** GLiNER2 extracts entities (subject="user", object="back"), creates them, returns in response
**Actual:** `/extract` crashes with NoneType error; entities come back as null; no facts can be staged

## Reproduction

**Environment:** Pre-prod (docker-host.helpalicekpro.ca)
**FaultLine version:** v1.0.9 (latest)
**Date discovered:** 2026-05-12 23:08:25 UTC

**Steps:**
```bash
curl -X POST http://192.168.1.10:8001/extract \
  -H 'Content-Type: application/json' \
  -d '{"text": "what'\''d I do to my back?"}'
```

**Response:**
```json
{
  "entities": [
    {
      "subject": null,
      "object": null,
      "rel_type": "affected_body_part",
      "subject_type": "Person",
      "object_type": "Person"
    }
  ]
}
```

## Evidence

**FaultLine logs (pre-prod, 2026-05-12 23:08:25):**
```
2026-05-12 23:08:25 [error    ] ingest.gliner2_failed          error="'NoneType' object has no attribute 'lower'"
```

**Impact sequence:**
1. ✓ Filter inlet called, message "what'd I do to my back?" processed
2. ✓ POST `/extract` endpoint called (GLiNER2 extracts "affected_body_part" rel_type)
3. ✗ Entity extraction fails: attempting to call `.lower()` on None
4. ✗ Response returns null subject/object (entity creation failed)
5. ✗ LLM rewrite_to_triples() receives null values, returns empty array []
6. ✗ No facts staged; extraction complete failure

## Root Cause Analysis

**IDENTIFIED:** Code in `src/api/main.py` (ingest or extract endpoint) is calling `.lower()` on an entity subject or object without null-checking first.

The GLiNER2 model correctly identifies the relationship type (`affected_body_part`) and entity types (`Person`), but the entity names/IDs themselves are coming back as None, causing downstream code to crash when attempting normalization (lowercasing).

**Possible locations in code:**
1. **`/extract` endpoint:** After GLiNER2 returns entities, entity fields are processed (lowercase, normalization)
2. **Entity registry resolution:** When attempting to resolve or create entities, None value causes crash
3. **Entity name normalization:** Line with `.lower()` on subject/object not checking for None first

**Why this breaks extraction:**
- GLiNER2 detects relationship but not entity strings
- Code tries to normalize (lowercase) entity without checking if it exists
- NoneType.lower() crashes
- Response with null entities returned to Filter
- Filter can't create edges with null subjects/objects
- No facts extracted

## Impact

**Severity: CRITICAL**

- **Scope:** Any medical/personal context extraction (burns, injuries, body parts, health conditions)
- **User-facing:** Zero facts extracted for medical questions; generic responses only
- **Data loss:** No medical context persisted; user context lost
- **Blocker:** Medical extraction completely non-functional; v1.0.9 deployment blocked

## Timeline

- **2026-05-12 22:57:** User tests "what'd I do to my back?"
- **2026-05-12 22:57:** OpenWebUI logs show `raw_triples=[]` (empty extraction)
- **2026-05-12 23:08:** Direct `/extract` test reveals NoneType crash in logs
- **2026-05-12 23:08–present:** Root cause isolated to `.lower()` on None value

## Affected Components

**Direct:**
- `src/api/main.py` — `/extract` endpoint and/or entity processing logic
- Entity registry or GLiNER2 response handler

**Indirect:**
- Filter's rewrite_to_triples() (receives null entities, returns [])
- Ingest pipeline (no edges to commit)
- Medical fact extraction (completely blocked)

## Investigation Scope for Deepseek

**Code review required:**

1. **`src/api/main.py` — `/extract` endpoint**
   - Search for all `.lower()` calls on entity subject/object values
   - Identify which one(s) lack null checks
   - Check: Is entity coming from GLiNER2 model output? From registry? From request parsing?

2. **Entity registry resolution**
   - How are extracted entities being resolved/created?
   - Are null values being checked before processing?
   - Is EntityRegistry.resolve() being called without validation?

3. **GLiNER2 response parsing**
   - After GLiNER2 model.extract_structured() returns, how is response handled?
   - Are entity names/IDs validated before being passed to entity creation?

**Test execution required:**

1. Run the exact `/extract` curl command locally and capture full stack trace
2. Identify the exact line number in `src/api/main.py` causing the crash
3. Trace backward to see why entity subject/object is None
4. Add null checks and re-test

## Solution

**Immediate fix:**
1. Find all `.lower()` calls on entity values in `/extract` endpoint
2. Add null checks: `if entity_subject: entity_subject.lower()` or equivalent
3. Return proper error response instead of null entities if entity extraction fails
4. Test locally with pytest
5. Deploy to pre-prod

**Testing:**
```bash
# Should return valid entities, not null
curl -X POST http://192.168.1.10:8001/extract \
  -H 'Content-Type: application/json' \
  -d '{"text": "what'\''d I do to my back?"}'

# Should extract medical facts
curl -X POST http://192.168.1.10:8001/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "what'\''d I do to my back?", "user_id": "test", "source": "test", "edges": []}'
```

## Success Criteria

- [ ] Find the `.lower()` line causing the NoneType crash
- [ ] Add null check to prevent crash
- [ ] `/extract` returns valid entities with subject/object names (not null)
- [ ] `rewrite_to_triples()` receives valid entities, returns non-empty triples
- [ ] Medical facts can be staged and persisted
- [ ] All tests pass locally (pytest)
- [ ] Pre-prod validation: "what'd I do to my back?" extracts medical facts

---

## Upon Completion

Update `scratch.md` with:

```
## #deepseek: dBug-017 GLiNER2 NoneType Crash — FIXED ✓

**Fix:** Added null checks to entity.lower() calls in src/api/main.py /extract endpoint.
**Line:** [exact line number]
**Commit:** [commit hash]

Medical extraction now working. "what'd I do to my back?" extracts entities properly.
```

Then test on pre-prod and report back.
