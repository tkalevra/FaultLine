# dBug-report-008: Duplicate Facts in /query Response + bob Disappearing

**Date:** 2026-05-14  
**Severity:** P2 (Data accuracy + consistency)  
**Status:** Investigation complete — query deduplication issue identified

## Symptom

"Tell me about my family" conversation shows inconsistent results:
1. **First response:** "You have two children: alice is 12, charlie is 19" — bob mentioned later as "Your daughter bob is ten"
2. **Subsequent prompts:** bob disappears from responses entirely
3. **Expected:** Consistent mention of all three children (alice, charlie, bob)

## Investigation Findings

### Backend Data (Verified Clean)
```sql
-- Facts table (all correct):
chris parent_of alice (conf=1, A)
chris parent_of charlie (conf=1, A)
bob child_of chris (conf=1, A)
chris spouse emma (conf=1, A)

-- Entity attributes (ages):
alice: 12, charlie: 19, bob: 10

-- No bidirectional conflicts
-- No wrong entities
```

### /query Endpoint Response (Problem Found!)
```json
Total facts returned: 36
Family facts duplicated:
  user parent_of alice (conf=1.0)
  chris parent_of alice (conf=1.0)  ← DUPLICATE, same relationship
  user parent_of charlie (conf=1.0)
  chris parent_of charlie (conf=1.0) ← DUPLICATE, same relationship
  bob child_of user (conf=1.0)
  bob child_of chris (conf=1.0)  ← DUPLICATE, same relationship
```

### Root Cause

The `/query` endpoint is returning facts with **dual subject display names:**
- Facts where subject is user (UUID: 3f8e6836...) displayed as both "user" and "chris"
- Produces duplicate family relationships in response

This breaks the expected behavior of **dprompt-61 deduplication** which should return ONE fact per `(subject_id, rel_type, object_id)` triple.

**Likely source:** The response builder is not properly normalizing subject/object display names before deduplication. Facts with the same UUID but different display aliases are counted as separate facts.

## Test Case

**Query:** "How many children do I have?"

**Current response inclualice:**
```
user parent_of alice
chris parent_of alice     ← Same fact, different display name
user parent_of charlie
chris parent_of charlie   ← Same fact, different display name
```

**Expected response:**
```
chris parent_of alice     ← Single fact, normalized display
chris parent_of charlie   ← Single fact, normalized display
```

## Impact

1. **LLM Confusion:** Duplicate facts may cause LLM to miscount children or ignore bob (sibling edges might take priority)
2. **Deduplication Failure:** dprompt-61 deduplication not working as intended
3. **Response Inconsistency:** Facts disappear on subsequent queries as LLM context shifts

## Affected Components

1. **`/query` endpoint response building** — returning duplicate display names for same fact
2. **dprompt-61 deduplication logic** — not normalizing subject/object display before dedup

## Files Involved

- `src/api/main.py` — `/query` endpoint, response building, fact filtering
- `openwebui/faultline_tool.py` — Filter receives these duplicates, may not handle them

## Next Steps

**Immediate fix required:** In `/query` response building, normalize subject/object display names to single preferred alias BEFORE returning to Filter. Ensure deduplication by `(subject_id, rel_type, object_id)` removes display-name duplicates.

---

**Status:** Valid bug. Requires code fix in dprompt-61 response normalization.

**Validation Method:**
```bash
curl -X POST http://192.168.1.10:8001/query \
  -d '{"user_id":"3f8e6836-72e3-43d4-bbc5-71fc8668b070","text":"How many children do I have"}' \
  | jq '.facts | map(select(.rel_type=="parent_of")) | unique_by(.subject + .rel_type + .object) | length'
# Expected: 2 (alice, charlie)
# Current: likely > 2 due to duplicates
```

