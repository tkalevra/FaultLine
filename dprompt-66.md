# dprompt-66: Query Deduplication Fix (dBug-report-008)

**Date:** 2026-05-14  
**Severity:** P2 (Response accuracy)  
**Status:** Specification complete

## Problem

`/query` endpoint returns duplicate family facts with different display names, causing inconsistent LLM responses:

**Symptom:** Family query mentions "two children" initially, then Gabby later, then disappears on subsequent prompts.

**Root Cause:** Line 3732 in `src/api/main.py`:
```python
pg_keys = {(f["subject"], f["object"], f["rel_type"]) for f in resolved_direct}
```

Builds deduplication key using **display names** (e.g., "chris", "user"), not UUIDs. Same fact from different sources appears with different display aliases, both pass dedup check and appear in response.

**Example:**
- `direct_facts` returns: "chris parent_of des"
- `baseline_facts` returns: "user parent_of des" (normalized by `_resolve_display_names`)
- Both have same `(subject_id="3f8e6836...", rel_type="parent_of", object_id="0638cc40...")` UUID
- But different display names → different pg_keys entry → both added to merged_facts
- Result: Facts returned twice, Filter/LLM confused

## Solution

Use **UUID keys** for initial fact merging (lines 3732-3746), not display names. Only deduplicate display names for the final pass.

**Step-by-step:**
1. When building `pg_keys` at line 3732, use `_subject_id` and `_object_id` preserved by `_resolve_display_names()`
2. Update dedup checks at lines 3734-3746 to use UUID keys
3. Keep the final dprompt-61 deduplication loop (lines 3821-3827) unchanged — it already uses UUID dedup correctly
4. Result: Display-name duplicates are filtered out before the final response

## Files to Modify

- `src/api/main.py` — `/query` endpoint, lines 3726–3750
  - Change `pg_keys` construction to use `_subject_id` / `_object_id`
  - Update dedup checks for resolved_qdrant and resolved_baseline merges

## Changes Required

**Before (line 3732):**
```python
pg_keys = {(f["subject"], f["object"], f["rel_type"]) for f in resolved_direct}
```

**After:**
```python
pg_keys = {(f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"]) for f in resolved_direct}
```

**Lines 3735-3737 and 3743-3745:** Update key construction similarly:
```python
# OLD:
key = (f["subject"], f["object"], f["rel_type"])

# NEW:
key = (f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"])
```

## Success Criteria

✅ Facts deduplicated by UUID, not display names  
✅ Same fact appears once in response (no "chris parent_of des" + "user parent_of des" duplicates)  
✅ Family query consistent: all three children (Des, Cyrus, Gabby) mentioned in all responses  
✅ Tests: 114+ pass, 0 regressions  
✅ Curl validation: `/query` returns 2 parent_of facts (des, cyrus), not 4+

## Test Case

```bash
curl -X POST http://192.168.40.10:8001/query \
  -d '{"user_id":"3f8e6836-72e3-43d4-bbc5-71fc8668b070","text":"How many children do I have"}' \
  | jq '.facts | map(select(.rel_type=="parent_of")) | unique_by(.subject+.rel_type+.object) | length'
# Expected: 2 (des, cyrus)
# Current (broken): 4+ (duplicates with different display names)
```

---

**Reference:** `BUGS/dBug-report-008.md`

