# dBug-PREFERRED-NAMES-EMPTY: Backend /query Returns Empty preferred_names Dict

**Severity:** High — Family facts injected but appear as UUIDs instead of display names (violates CLAUDE.md UUID constraint)

**Status:** INVESTIGATION

**Date:** 2026-05-22

**User Context:** User ${USER} (${TEST_USER_ID})

## Summary

**Filter injection now works** (dBug-FAMILY-FACTS-NOT-INJECTED fixed in commit 4e86a9a), but family facts are being injected as raw UUIDs instead of resolved display names.

**Problem:** Backend `/query` endpoint returns `preferred_names: {}` (empty dict) when facts contain UUIDs.

**Impact:** 
- Family facts in system message appear as: `spouse=Fb0868C4-12B4-587D-9A3B-Ce96Ca5979Ca` (UUID)
- Should appear as: `spouse=${SPOUSE}` (display name)
- Violates CLAUDE.md constraint: "No UUIDs in user-facing output to LLM"
- LLM interprets UUIDs as "redacted" and refuses to use family information

## Evidence

**Test query:**
```bash
curl -X POST "http://${BACKEND_IP}:8001/query" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "${TEST_USER_ID}",
    "text": "tell me about my family"
  }' | jq '.preferred_names'

# Returns: {}
```

**Backend facts returned (correct):**
```json
{
  "subject": "${TEST_USER_ID}",
  "object": "fb0868c4-12b4-587d-9a3b-ce96ca5979ca",
  "rel_type": "spouse"
}
```

**Filter injection log (incorrect UUIDs):**
```
[FaultLine Filter] injecting system message:
- ⊢ FaultLine Memory (from long-term knowledge graph):
- family: spouse=Fb0868C4-12B4-587D-9A3B-Ce96Ca5979Ca, children=17Deeee3-1600-56A5-92E0-67A7423E00Bb, ...
```

**Expected (would require preferred_names populated):**
```
- family: spouse=${SPOUSE}, children=${CHILD1}, ${CHILD3}, ${CHILD2}, ${CHILD3}
```

## Root Cause Analysis

Code at `src/api/main.py` lines 6568-6574 attempts to populate `preferred_names`:

```python
if registry:
    for f in merged_facts:
        subject_uuid = f.get("_subject_id", f["subject"])
        object_uuid = f.get("_object_id", f["object"])
        if subject_uuid not in preferred_names and subject_uuid != user_entity_id_for_query:
            preferred_names[subject_uuid] = f["subject"]  # Already-resolved display name
        if object_uuid not in preferred_names and object_uuid != user_entity_id_for_query:
            preferred_names[object_uuid] = f["object"]  # Already-resolved display name
```

**Suspected issue:** 
1. Facts may not have `_subject_id`/`_object_id` metadata fields set
2. Code falls back to using `f["subject"]` and `f["object"]` as both keys and values
3. But facts at this point still contain UUIDs, not display names (no `_resolve_display_names()` call?)
4. Result: `preferred_names` dict populated with UUID→UUID mappings, which doesn't help the filter

**Hypothesis:**
- `_resolve_display_names()` should convert UUIDs to display names AND populate `_subject_id`/`_object_id` metadata
- If it's not being called before the preferred_names population code, facts will still be UUIDs

## Questions

1. **Is `_resolve_display_names()` being called on merged_facts before line 6564?**
   - If yes, why aren't `_subject_id`/`_object_id` fields present?
   - If no, why not?

2. **What should preferred_names dict keys/values be?**
   - Keys: UUIDs (for lookup from fact.subject/object)
   - Values: Display names (for filter to inject)
   - Current: `{}` (empty)

3. **Is the issue entity registry initialization?**
   - Code checks `if registry:` before populating preferred_names
   - Is registry None for this user?

## Next Steps

1. Add debug logging to preferred_names population block to confirm it's executing
2. Inspect what `_subject_id`/`_object_id` fields contain (if present)
3. Verify `_resolve_display_names()` is being called and completing successfully
4. Check if entity registry is properly initialized for the user
5. Trace the full facts pipeline from baseline facts → merged facts → preferred_names population

## Related Issues

- **dBug-FAMILY-FACTS-NOT-INJECTED** (FIXED 4e86a9a) — Filter was filtering out family facts entirely
- **dBug-EMBEDDING-ENDPOINT** (FIXED 8b1dcf0) — Embedding endpoint path incorrect

## Constraint Violation

**CLAUDE.md:** "No UUIDs in user-facing output to LLM"

Current behavior violates this. Filter receives UUIDs and injects them directly, causing LLM to treat them as redacted/blocked information.
