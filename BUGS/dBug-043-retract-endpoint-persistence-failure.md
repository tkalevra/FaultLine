# dBug-043: /retract Endpoint Returns Success But Facts NOT Superseded in Database

**Status**: ✅ FIXED  
**Severity**: CRITICAL (Data Integrity)  
**Affected Component**: `src/api/main.py` `/retract` endpoint  
**Date Reported**: 2026-05-17 20:17 UTC  
**Fixed**: 2026-05-18 via dprompt-106/107 (metadata-driven retraction detection)  
**Production Deployed**: 2026-05-18 commit 0437f1a  
**Solution**: Replaced LLM-based retraction detection with pattern-based metadata lookup (dprompt-115 unified gate, dprompt-116 pattern-driven filtering)

## Summary

The `/retract` endpoint returns HTTP 200 OK and logs success, but the facts in the PostgreSQL database are **NOT actually superseded**. User receives retraction confirmation ("✓ Understood. I've removed Robert from my memory...") but the fact rows remain with `superseded_at = NULL`, meaning the retraction never persisted.

This is a **critical data integrity issue**: users believe facts are removed, but they remain in the system and will continue to be queried/returned.

## Test Case That Triggered Bug

**Test Environment**: Production docker-host.helpalicekpro.ca  
**User**: John Thompson (uuid: ${TEST_USER_ID})  
**Date**: 2026-05-17 20:17:12 UTC

**Steps**:
1. Sanitized database: Deleted all Robert references
2. Ingested: "My son Robert is 10 years old."
   - ✅ Robert created (uuid: eb3639e3-da33-59ff-8e93-c6293305e060)
   - ✅ age=10 stored in entity_attributes
   - ✅ parent_of/child_of relationships created in facts
3. Retracted: "Please delete Robert from my memory. He is not my son."
   - ✅ `/extract/retraction` endpoint called, returned 200 OK
   - ✅ Filter logs: "retraction.semantic method=semantic level=granular confidence=1.0"
   - ✅ `/retract` endpoint called, returned 200 OK
   - ✅ User shown confirmation: "✓ Understood. I've removed Robert from my memory as you requested."
   - ❌ **Database still shows facts with superseded_at = NULL**

## Evidence

### Database Query (After Retraction)

```sql
SELECT rel_type, subject_id, object_id, superseded_at 
FROM facts 
WHERE (subject_id = 'eb3639e3-da33-59ff-8e93-c6293305e060' 
    OR object_id = 'eb3639e3-da33-59ff-8e93-c6293305e060')
  AND rel_type IN ('parent_of', 'child_of');

-- Result:
rel_type  |              subject_id              |              object_id               | superseded_at 
-----------+--------------------------------------+--------------------------------------+---------------
 child_of  | eb3639e3-da33-59ff-8e93-c6293305e060 | ${TEST_USER_ID} | [NULL]
 parent_of | ${TEST_USER_ID} | eb3639e3-da33-59ff-8e93-c6293305e060 | [NULL]
```

**Expected**: Both rows should have `superseded_at = NOW()` timestamp  
**Actual**: Both rows show empty/NULL `superseded_at`

### FaultLine Logs

```
2026-05-17 20:17:12 [warning  ] retract.qdrant_sync_flag_failed 
  error='syntax error at or near "LIMIT"\nLINE 3:                        LIMIT 1000\n                               ^\n' 
  user_id=${TEST_USER_ID}

INFO:     172.16.23.1:55484 - "POST /retract HTTP/1.1" 200 OK
```

The endpoint returns 200 OK immediately after logging a SQL syntax error. This suggests:
- The retraction UPDATE query may have succeeded
- The transaction may NOT have been committed
- OR the Qdrant sync flag update is failing AFTER the response is sent (race condition)

### Filter Logs

```
[FaultLine Filter] retraction.semantic method=semantic level=granular category=None confidence=1.0
[FaultLine Filter] subject_resolution_failed: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed
HTTP Request: POST http://${BACKEND_IP}:8001/retract "HTTP/1.1 200 OK"
[FaultLine Filter] /query status=200
[FaultLine Filter] filtered: 74/74 facts
```

Filter shows:
1. Retraction WAS detected ✅
2. Attempted to resolve subject for confirmation message ❌ (PostgreSQL socket error - Filter can't reach DB from OpenWebUI container)
3. Called `/retract` - received 200 OK ✅
4. Proceeded to `/query` anyway (because retraction confirmation failed silently)

## Call Chain Breakdown

```
Filter inlet() 
  ↓
_detect_retraction_intent()
  ✅ Returns: (True, {is_retraction: true, subject: "robert", ...})
  ↓
_fire_retract() 
  ✅ POST /retract → returns 200 OK + retraction response JSON
  ↓
Extract retraction subject UUID for confirmation message
  ❌ FAILS: PostgreSQL socket error (Filter running in OpenWebUI container)
  ↓ (error caught, silently continues)
  ↓
Confirmation message still injected (generic: "✓ Understood. I've removed Robert...")
  ↓
User thinks retraction succeeded
  ✅ Response returned to user
  
Meanwhile in Database:
  ❌ /retract endpoint returned 200 OK
  ❌ BUT facts still have superseded_at = NULL
  ❌ Retraction never persisted
```

## Root Cause Analysis

### Issue 1: `/retract` Endpoint SQL Syntax Error

**Location**: `src/api/main.py`, line 5818+  
**Symptom**: Log shows `retract.qdrant_sync_flag_failed error='syntax error at or near "LIMIT"'`

**Hypothesis**: After executing the UPDATE to set `superseded_at = now()` (line 5858–5888), the endpoint attempts to update a Qdrant sync flag with a LIMIT clause that has incorrect syntax.

**Code section**: Lines 5858–5922 in scope-aware retraction path
```python
# Update facts (lines 5858-5887)
query = "UPDATE facts SET superseded_at = now() WHERE user_id = %s AND superseded_at IS NULL"
# ... add conditions ...
cur.execute(query, params)
retracted_count = cur.rowcount

# Commit (line 5922)
db.commit()

# Somewhere after this, likely in legacy path (lines 5924+) or via manager.retract():
# A query with LIMIT is being executed and failing
```

**Question**: Is the transaction committing BEFORE the Qdrant flag update attempt? If so, the UPDATE should have persisted even if the flag update fails.

### Issue 2: Filter Cannot Resolve Entity Names

**Location**: OpenWebUI Filter in openwebui/faultline_function.py  
**Symptom**: "subject_resolution_failed: connection to server on socket /var/run/postgresql/.s.PGSQL.5432 failed"

**Cause**: Filter is attempting to connect to PostgreSQL via Unix socket `/var/run/postgresql/.s.PGSQL.5432`, but:
- Filter runs in OpenWebUI container
- PostgreSQL runs on host or separate container
- Unix socket is not mounted/available to OpenWebUI container

**Impact**: Retraction confirmation message cannot be personalized (e.g., "I've removed {subject} from my memory...") because subject resolution fails. The generic confirmation is used instead, masking the failure from the user.

## Test Validation Command

To reproduce:

```bash
# 1. Sanitize database
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"DELETE FROM entity_aliases WHERE alias='robert';
   DELETE FROM entity_attributes WHERE entity_id='eb3639e3-da33-59ff-8e93-c6293305e060';
   DELETE FROM facts WHERE subject_id='eb3639e3-da33-59ff-8e93-c6293305e060' OR object_id='eb3639e3-da33-59ff-8e93-c6293305e060';
   DELETE FROM entities WHERE id='eb3639e3-da33-59ff-8e93-c6293305e060';\""

# 2. Ingest Robert
curl -s -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-1cf72f713e884a06b3dab80a8a003669" \
  -H "Content-Type: application/json" \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "My son Robert is 10 years old."}], "stream": false}'

# 3. Retract Robert
curl -s -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer sk-1cf72f713e884a06b3dab80a8a003669" \
  -H "Content-Type: application/json" \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "Please delete Robert from my memory. He is not my son."}], "stream": false}'

# 4. Verify facts are NOT superseded (confirms bug)
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
  \"SELECT rel_type, superseded_at FROM facts WHERE subject_id='eb3639e3-da33-59ff-8e93-c6293305e060';\""
```

## Related Issues

- **dBug-042**: Retraction negation pattern detection (UPDATED to show Layer 1 LLM detection is primary blocker)
- **dBug-041**: Correction handling (is_correction flag propagation)
- **dBug-016**: OpenWebUI NoneType crash (fixed via chat_id injection)

## Impact Assessment

**Severity**: CRITICAL
- **Data Integrity**: Facts remain in database alicepite user-initiated retraction
- **User Trust**: Users believe corrections/removals are applied but system silently ignores them
- **Query Correctness**: Retracted facts will still be returned in `/query` calls
- **Qdrant Cache**: Stale facts may remain in Qdrant if Qdrant cleanup failed

**Scope**: All retraction attempts via Filter inlet
- Affects all users
- Affects all rel_types (family, work, location, etc.)
- No scope limitation

## Investigation Steps

1. **Verify UPDATE persistence**: Insert diagnostic log statement in `/retract` endpoint AFTER `db.commit()` to confirm commit succeeded
   ```python
   db.commit()
   logger.info(f"retract.commit_done rows_affected={retracted_count} user_id={req.user_id}")
   ```

2. **Isolate Qdrant flag error**: The LIMIT syntax error appears to be in a separate code path. Search for:
   ```bash
   grep -n "LIMIT 1000" src/api/main.py
   grep -n "qdrant_sync_flag" src/api/main.py
   ```

3. **Test scope-aware vs legacy path**: 
   - Current test uses scope-aware path (lines 5837–5922)
   - Legacy path (lines 5924+) uses `FactStoreManager.retract()`
   - Determine which path is being taken for "Please delete Robert..." message

4. **Check transaction isolation**: Are there uncommitted transactions blocking the UPDATE?
   ```bash
   ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline -c \
     \"SELECT * FROM pg_stat_activity WHERE state='idle in transaction';\""
   ```

## Proposed Fix Areas

1. **Ensure UPDATE persistence**: Verify `db.commit()` is executing and transaction is not being rolled back
2. **Fix LIMIT syntax error**: Find and correct the malformed SQL with LIMIT clause
3. **Add diagnostic logging**: Log UPDATE row count, commit status, Qdrant sync status
4. **Handle commit failures gracefully**: If commit fails, return HTTP 500, not 200
5. **Filter subject resolution**: Either pass PostgreSQL DSN to Filter container, or delegate subject resolution to backend

## UPDATE: Additional Findings (2026-05-17 21:30 UTC)

### Categorical Retraction Test Results

New test with **taxonomy inference now working** (dBug-044 FIXED):

**Test**: Ingest pet facts + attempt categorical retraction

```
Ingest:     has_pet(emma, fraggle) → committed ✓
Taxonomy:   pets taxonomy created ✓ (dBug-044 fix working)
Retraction: "forget about them" (pets category)
Result:     rowcount=0 (NO FACTS SUPERSEDED)
```

**Logs show**:
```
extract.retraction_success is_retraction=True scope_level=categorical category='pets'
retract.commit_done rowcount=0 scope_level=categorical
```

### Root Cause Narrowed: Categorical Scope Resolution

The issue is **NOT** about whether taxonomies exist (dBug-044 fixed that). The issue is:

**The `/retract` endpoint receives the categorical scope but returns rowcount=0**, meaning:
1. Taxonomy `pets` is found ✓
2. rel_types [`has_pet`] are determined ✓
3. **BUT the SQL query to find matching facts returns 0 rows** ✗

**Hypothesis**: The WHERE clause in `/retract` endpoint is using **display names instead of UUIDs** or some other issue in how it builds the match criteria.

### Suspect Code Path

**File**: `src/api/main.py`, `/retract` endpoint, categorical scope handling

The endpoint needs to:
1. Query `entity_taxonomies` for the category (`pets`)
2. Get the `rel_types_defining_group` (e.g., `["has_pet"]`)
3. Build SQL: `UPDATE facts SET superseded_at=now() WHERE rel_type IN ('has_pet') AND user_id=%s`
4. Execute and commit

**The fact that rowcount=0 suggests** either:
- The SQL WHERE clause is malformed
- It's querying the wrong user_id
- The rel_types list is empty or wrong
- Display names are being compared instead of rel_type strings

### Test Evidence Chain

```
Filter (inlet):
  ✅ Detects retraction: text contains "forget about them"
  ✅ Determines category: "pets" (extracted by LLM)
  ✅ Calls POST /retract with scope={'category': 'pets'}
  
Backend (/retract):
  ✅ Receives categorical scope request
  ✅ Logs: retract.commit_done rowcount=0
  ❌ No facts found to supersede

Database (facts table):
  ✅ Contains: 1 has_pet fact with rel_type='has_pet'
  ✅ User_id matches request
  ❌ But UPDATE returns rowcount=0 (WHERE clause didn't match)
```

## Status

- **Root Cause of dBug-044**: ✅ FIXED (taxonomy inference now working)
- **Root Cause of dBug-043**: IDENTIFIED but not yet fixed (categorical scope query failing)
  - Taxonomies exist ✓
  - Retraction detected ✓
  - WHERE clause in /retract endpoint needs investigation
- **Next Step**: Debug `/retract` endpoint categorical scope SQL query building
