# dprompt-66b: DEEPSEEK_INSTRUCTION_TEMPLATE — Query Deduplication Fix

## Task

Fix `/query` endpoint deduplication logic to use UUID keys instead of display names, eliminating duplicate facts with different display aliases.

## Context

dBug-report-008 discovered that `/query` returns duplicate family facts:
- "chris parent_of des" AND "user parent_of des" (same subject_id, different display)
- LLM receives conflicting versions, counts children wrong, forgets Gabby on later turns

**Root cause:** Line 3732 builds `pg_keys` using display names as dedup key:
```python
pg_keys = {(f["subject"], f["object"], f["rel_type"]) for f in resolved_direct}
```

But `f["subject"]` is a display name (e.g., "chris" or "user"), not a UUID. Same UUID with different aliases creates separate entries.

**Why it matters:** Dprompt-61 preserved `_subject_id` and `_object_id` (UUIDs) for deduplication, but initial merging logic doesn't use them. Fix: use UUIDs for initial pg_keys deduplication.

**Integration:** Local code fix only. No schema, migration, or API contract changes. dprompt-61 dedup loop stays unchanged.

**Reference:** `dprompt-66.md`, `BUGS/dBug-report-008.md`

## Constraints

### MUST:
- Change pg_keys construction (line 3732) to use UUID keys from `_subject_id` and `_object_id`
- Update dedup checks at lines 3734-3746 (resolved_qdrant and resolved_baseline merges) to use UUID keys
- Keep the final dprompt-61 dedup loop (lines 3821-3827) unchanged
- Test locally: `pytest tests/api/test_query.py -v`, 114+ pass, 0 regressions
- Validate: curl test returns exactly 2 parent_of facts (no duplicates with different display names)

### DO NOT:
- Refactor query logic or add new features
- Change `_resolve_display_names()` function
- Modify the final dprompt-61 deduplication block
- Change the response schema or API contract

### MAY:
- Add inline comment explaining UUID vs display name keys
- Include performance notes if UUID key matching adds latency

## Sequence

### 1. Read & Understand (No coding)

- Read `dprompt-66.md` (specification, problem analysis)
- Read `BUGS/dBug-report-008.md` (investigation findings, curl validation)
- Read `src/api/main.py`:
  - Lines 3726–3750: Locate the pg_keys construction and merge logic
  - Lines 3821–3840: Verify the final dprompt-61 dedup block is unchanged
  - Confirm: What are `_subject_id` and `_object_id`? Where are they set?

Confirm: Do you understand why using display names for pg_keys causes duplicates?

### 2. Apply Fix

**File:** `src/api/main.py`, `/query` endpoint

**Location:** Lines 3732–3746 (pg_keys construction and merge dedup checks)

**Changes:**

**Line 3732 (pg_keys construction):**
```python
# OLD:
pg_keys = {(f["subject"], f["object"], f["rel_type"]) for f in resolved_direct}

# NEW:
pg_keys = {(f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"]) for f in resolved_direct}
```

**Line 3735 (resolved_qdrant dedup check):**
```python
# OLD:
key = (f["subject"], f["object"], f["rel_type"])

# NEW:
key = (f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"])
```

**Line 3743 (resolved_baseline dedup check):**
```python
# OLD:
key = (f["subject"], f["object"], f["rel_type"])

# NEW:
key = (f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"])
```

**Optional:** Add inline comment at line 3732:
```python
# Use UUID keys for dedup, not display names. Same UUID may have different aliases
# (e.g., "chris" vs "user" for user entity). UUID dedup ensures no display-name duplicates.
```

### 3. Test Locally

**Run test suite:**
```bash
pytest tests/api/test_query.py -v
```

Expected: 114+ tests pass, 0 regressions.

**Spot-check with curl:**
```bash
curl -X POST http://192.168.40.10:8001/query \
  -H "Content-Type: application/json" \
  -d '{"user_id":"3f8e6836-72e3-43d4-bbc5-71fc8668b070","text":"How many children do I have"}' \
  | jq '.facts | map(select(.rel_type=="parent_of")) | unique_by(.subject+.rel_type+.object) | length'
```

Expected: 2 (des, cyrus only, no duplicates)

**Verify Gabby appears (via sibling_of relationships):**
```bash
curl -X POST http://192.168.40.10:8001/query \
  -H "Content-Type: application/json" \
  -d '{"user_id":"3f8e6836-72e3-43d4-bbc5-71fc8668b070","text":"Tell me about my family"}' \
  | jq '.facts | map(select(.subject=="gabby" or .object=="gabby")) | length'
```

Expected: ≥ 2 facts (gabby child_of user, sibling relationships)

### 4. STOP & Report

Update `scratch.md` with template below. Do NOT proceed to deployment.

## Deliverable

**Modified file:**
- `src/api/main.py` — pg_keys construction + merge dedup checks (3 locations, ~10 lines)

**Test results:**
- 114+ tests pass, 0 regressions
- Curl validation: parent_of facts correctly deduplicated

## Success Criteria

✅ pg_keys built using UUID keys (`_subject_id`, `_object_id`), not display names  
✅ resolved_qdrant merge dedup uses UUID keys  
✅ resolved_baseline merge dedup uses UUID keys  
✅ Family query returns unique facts (no "chris parent_of des" + "user parent_of des")  
✅ Gabby consistently appears in family responses  
✅ Tests: 114+ pass, 0 regressions  
✅ Curl validation: 2 parent_of facts, not 4+

## Upon Completion

**⚠️ MANDATORY: Update scratch.md with this template, then STOP:**

```markdown
## ✓ DONE: dprompt-66 (Query Deduplication Fix) — [DATE]

**Task:** Fix `/query` duplicate facts issue (different display names for same UUID).

**Implementation (src/api/main.py, lines 3732–3746):**
- Changed pg_keys construction to use `_subject_id`, `_object_id` instead of display names
  - Line 3732: pg_keys = {(f.get("_subject_id", ...), ...) ...}
  - Lines 3735, 3743: key = (f.get("_subject_id", ...), ...) for dedup checks
  
**Philosophy:** Deduplication must use stable identifiers (UUIDs), not display names (which vary by alias).

**Tests:**
- All 114+ existing tests pass ✓
- Curl validation: parent_of facts return 2 (no display-name duplicates) ✓
- Family query consistently mentions all three children ✓

**Validation:**
```bash
curl -X POST http://192.168.40.10:8001/query \
  -d '{"user_id":"3f8e6836-72e3-43d4-bbc5-71fc8668b070","text":"How many children"}' \
  | jq '.facts | map(select(.rel_type=="parent_of")) | unique_by(.subject+.rel_type+.object) | length'
# Result: 2 ✓
```

**AWAITING USER REBUILD AND VALIDATION.**
```

Then **STOP immediately** — do not proceed with live testing, wait for user direction.

## Critical Rules (Non-Negotiable)

**UUID vs display name:** All deduplication uses UUIDs from `_subject_id`/`_object_id`, not display names. Display names vary (chris, user, christopher), UUIDs don't.

**Scope lock:** Code change only. No logic refactoring, no dprompt-61 modifications.

**Test discipline:** 114+ existing tests must pass. Curl validation must show no duplicates.

**STOP clause mandatory:** Implementation ends with STOP. User rebuilds pre-prod, validates, then decides next.

---

**Template version:** 1.0 (follows DEEPSEEK_INSTRUCTION_TEMPLATE)  
**Philosophy:** Stable identifiers (UUIDs) for deduplication, display names for presentation.  
**Status:** Ready for execution by deepseek

