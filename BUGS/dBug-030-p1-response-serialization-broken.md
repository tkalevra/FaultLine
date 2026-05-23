# dBug-030: P1 Response Serialization Broken — /query Returns Empty Facts

**Status:** OPEN  
**Severity:** CRITICAL (P1 implementation non-functional)  
**Date Reported:** 2026-05-16 01:50 UTC  
**Related:** dBug-029 (P0 cleanup complete, P1 implementation regressed)  
**Impact:** Query returns 0 facts alicepite database containing 18+ active facts; LLM receives no family relationships for injection

---

## Broken Behavior

**Setup:** P0 cleanup executed (pronoun/stopword entities deleted). P1 implementation applied (Class C filtering + identity normalization).

**Test:** POST /query for user `${TEST_USER_ID}`, text "tell me about my family"

**Expected Response:**
```json
{
  "status": "ok",
  "facts": [
    {"subject": "${USER}", "rel_type": "parent_of", "object": "bob", "confidence": 0.8, ...},
    {"subject": "${USER}", "rel_type": "parent_of", "object": "charlie", "confidence": 0.8, ...},
    {"subject": "${USER}", "rel_type": "parent_of", "object": "alice", "confidence": 0.8, ...},
    {"subject": "${USER}", "rel_type": "spouse", "object": "emma", "confidence": 0.8, ...},
    ...
  ],
  "preferred_names": {"uuid1": "${USER}", "uuid2": "emma", ...},
  "attributes": {...}
}
```

**Actual Response:**
```json
{
  "status": "ok",
  "facts": [],
  "preferred_names": {},
  "canonical_identity": null,
  "attributes": {}
}
```

**LLM Impact:**
Filter receives empty facts array → injects no facts into system message → LLM responds:
```
"I can't see your family information from the FaultLine database context provided. 
The system only has limited entity data (John, charlie, ${ENTITY}) with attributes..."
```

---

## Database State (Verified)

```sql
SELECT COUNT(*) FROM facts WHERE user_id = '${TEST_USER_ID}';
-- Returns: 18 active facts

SELECT rel_type, COUNT(*) FROM facts 
WHERE user_id = '${TEST_USER_ID}'
GROUP BY rel_type;
-- pref_name: 10
-- parent_of: 3
-- child_of: 3
-- spouse: 1
-- also_known_as: 1
```

Facts exist and are correct. Issue is in response construction.

---

## Logs Show Facts Are Computed

Pre-rebuild logs (01:42:14) show `/query` working correctly:
```
query.initial_user_facts       count=33 rel_types={parent_of: 6, child_of: 6, spouse: 2, ...}
query.class_c_filtered         dropped=3 threshold=0.6
query.deduplicated             after=29 before=36
query.merged                   pg_hits=36 baseline=0 total=46
```

This proves:
1. Facts are fetched from PostgreSQL ✓
2. Class C filtering runs ✓
3. Deduplication completes (29 facts) ✓
4. **But response returns 0 facts** ✗

---

## Root Causes (Suspected)

### Issue 1: Exception in Response Construction Path
Between line 4620 (`query.deduplicated` log) and line 4632 (return statement in `/query`), an exception may be silently caught by the except block at line 4638, which returns empty facts.

**Candidates:**
- `_build_entity_types()` at line 4629 throwing exception
- JSON serialization of `_aliases` metadata failing (new in P1)
- Fact stripping (lines 4625-4627) removing required fields

**Evidence:**
- No `query.failed` log appears (would log error if exception caught)
- No `query.ok` log appears in current session (was present pre-rebuild)
- Response construction reaches except block silently

### Issue 2: Deduplication Logic Clearing Facts
Lines 4602-4619 (dedup + alias attachment):
```python
_deduped: dict[tuple, dict] = {}
for _f in merged_facts:
    _sid = _f.pop("_subject_id", ...)  # removes key from dict
    _oid = _f.pop("_object_id", ...)
    ...
_aliased_facts = [_f for (_sid, _rel, _oid), _f in _deduped.items()]
merged_facts = _aliased_facts  # reassigned
```

If `_deduped` is empty, `_aliased_facts` will be empty → empty response.

### Issue 3: Identity Normalization Removing All Facts
Lines 4586-4596 normalize user identity UUIDs. If `user_entity_ids_for_query` is undefined or empty, normalization might fail silently and clear facts.

---

## For DEEPSEEK

**Investigate:**

1. **Add debug logging before return statement (line 4631):**
   ```python
   log.info("query.response_construction", 
            merged_facts_count=len(merged_facts),
            entity_types_count=len(entity_types),
            preferred_names_count=len(preferred_names))
   ```
   Execute test and check logs to confirm facts exist before return.

2. **Verify `_build_entity_types()` doesn't throw:**
   - Add try/except with logging around line 4629
   - Check if exception silently clears response

3. **Verify dedup produces non-empty `_deduped`:**
   - Add log at line 4609: `log.info("query.dedup_dict_size", size=len(_deduped))`
   - Confirm _deduped has 29 items before building _aliased_facts

4. **Check identity normalization doesn't clear facts:**
   - Log `user_entity_ids_for_query` value at line 4587
   - Verify normalization loop at lines 4590-4596 doesn't clear facts

5. **Run test and provide:**
   - Full logs from `/query` call showing debug output
   - Whether `query.failed` appears (indicates exception)
   - Size of merged_facts at each transformation step

**Deliverables:**
- Root cause identified (which step clears facts)
- Fixed code
- Verification: `/query` returns 29 facts, LLM receives family relationships

---

## Test Procedure (for verification after fix)

```bash
# Test /query directly
curl -X POST "http://localhost:8001/query" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "${TEST_USER_ID}",
    "text": "tell me about my family"
  }' | jq '.facts | length'
# Expected: 29 (or similar non-zero count)

# Test through OpenWebUI
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "faultline-test",
    "messages": [{"role": "user", "content": "tell me about my family"}],
    "stream": false
  }' | jq '.choices[0].message.content'
# Expected: Response mentions spouse emma, children charlie/bob/alice, location info
```

---

## Definition of Fixed

dBug-030 is FIXED when:
- `/query` returns non-empty facts array (29+ facts for family query)
- Facts include parent_of, child_of, spouse relationships
- LLM response references spouse, children, and location (facts injected successfully)
- No `query.failed` or exceptions in logs
- P1 identity normalization and Class C filtering remain functional
