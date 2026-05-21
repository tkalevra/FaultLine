# dBug-020: Staged Facts Not Promoting to Long-Term Memory

**Status:** RESOLVED (Root Cause: dBug-016)  
**Severity:** CRITICAL (now unblocked)  
**Discovered:** 2026-05-14 (pre-prod deployment)  
**Resolved:** 2026-05-14 (via dBug-016 workaround)  
**Involves:** Re-embedder service, staged facts promotion mechanism, confirmed_count tracking  

## Summary

**NOT a promotion bug** — dBug-020 was a symptom of dBug-016 (OpenWebUI NoneType crash blocking extraction). Investigation revealed:
- Facts table: 0 rows (no facts ingested, not promotion failure)
- Root cause: Extraction calls failed with NoneType crash → no triples extracted → no ingest → nothing to promote
- Re-embedder: Already working correctly, just had zero facts to process

**Resolution:** Applied temporary socket/main.py patch to coerce chat_id None → '' (dBug-016 workaround via dprompt-83). Extraction now works, facts flow in, staging/promotion resumes normally.

**Original issue (below) is now moot** — documented for historical context.

## Root Causes (Ordered by Likelihood)

### 1. **Re-Embedder Service Not Running** (Most Likely)

The re-embedder is launched as a background daemon in `docker-entrypoint.sh` (line 19):

```bash
python -m src.re_embedder.embedder &
REEMBED_PID=$!
```

**To verify:** SSH to TrueNAS and check if the re-embedder process exists:

```bash
ssh docker-host -x "sudo docker exec faultline-wgm ps aux | grep -i embedder"
ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | tail -100" | grep -i "embedder\|error\|traceback"
```

**If re-embedder PID dies:** Check logs for errors during startup. Likely causes:
- `POSTGRES_DSN` misconfigured or database unavailable
- `QDRANT_URL` unreachable
- `QWEN_API_URL` unavailable for embedding calls
- Malformed environment variables

**See:** `docker-entrypoint.sh` lines 18–26; re-embedder health check runs at startup.

---

### 2. **Promotion Threshold Never Met** (Possible)

`promote_staged_facts()` in `src/re_embedder/embedder.py` (lines 302–370) promotes facts when:

```sql
fact_class = 'B' 
AND confirmed_count >= 3
AND promoted_at IS NULL
AND expires_at > now()
```

**How confirmed_count increments:**

- **Initial ingest:** `_commit_staged()` inserts fact with `confirmed_count = 0` (default from schema)
- **Duplicate ingest:** ON CONFLICT increments: `confirmed_count = staged_facts.confirmed_count + 1`

**Sequence for one fact to promote:**
1. Ingest 1 → INSERT → `confirmed_count = 0`
2. Ingest 2 (same fact) → ON CONFLICT → `confirmed_count = 1`
3. Ingest 3 (same fact) → ON CONFLICT → `confirmed_count = 2`
4. Ingest 4 (same fact) → ON CONFLICT → `confirmed_count = 3` ✓ **NOW ELIGIBLE**

**To verify:** Query the database directly:

```bash
ssh docker-host -x "sudo docker exec faultline-wgm psql \$POSTGRES_DSN -c \"
  SELECT 
    COUNT(*) as total_staged,
    COUNT(CASE WHEN fact_class = 'B' THEN 1 END) as class_b,
    COUNT(CASE WHEN confirmed_count >= 3 THEN 1 END) as promotion_eligible,
    COUNT(CASE WHEN confirmed_count >= 3 AND promoted_at IS NULL THEN 1 END) as ready_to_promote
  FROM staged_facts
  WHERE expires_at > now();
\""
```

If `ready_to_promote > 0` but facts aren't being moved to `facts` table, the issue is **#1 (re-embedder not running)**.

If `ready_to_promote = 0`, the issue is **#2 (facts never seeing duplicates)** — check if `/ingest` is being called at all.

---

### 3. **Ingest Not Triggering** (Less Likely)

The `/ingest` endpoint is only called when:
- Filter receives text ≥3 words, OR matches self-identification pattern (`my name is`, `I am`, etc.)

**To verify:** Check logs for ingest calls:

```bash
ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | grep -i 'ingest' | tail -20"
```

Expected output should show entries like:
```
ingest.class_a_committed count=X
ingest.class_b_staged count=X  
ingest.class_c_staged count=X
```

If NO ingest logs appear, the filter's `will_ingest` gate is blocking. Check:
- Message word count ≥ 3
- Filter is loaded in OpenWebUI
- Filter's `/extract` and LLM calls are completing successfully

---

## Debugging Checklist

Run these in order on the TrueNAS deployment:

### Step 1: Container Status

```bash
ssh docker-host -x "sudo docker ps | grep faultline"
ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | grep -i 'error\|started\|ready' | tail -20"
```

### Step 2: Re-Embedder Process

```bash
ssh docker-host -x "sudo docker exec faultline-wgm ps aux | grep -E 'python.*embedder|uvicorn'"
```

Expected output should show TWO processes:
- `python -m src.re_embedder.embedder`
- `uvicorn src.api.main:app`

### Step 3: Database Connectivity

```bash
ssh docker-host -x "sudo docker exec faultline-wgm psql \$POSTGRES_DSN -c 'SELECT now();'"
```

Should return current timestamp, not connection error.

### Step 4: Staged Facts Status

```bash
ssh docker-host -x "sudo docker exec faultline-wgm psql \$POSTGRES_DSN -c \"
  SELECT fact_class, COUNT(*) as count, MIN(confirmed_count) as min_confirmed, MAX(confirmed_count) as max_confirmed
  FROM staged_facts
  WHERE expires_at > now()
  GROUP BY fact_class;
\""
```

### Step 5: Facts Table (Check if ANY promotion happened)

```bash
ssh docker-host -x "sudo docker exec faultline-wgm psql \$POSTGRES_DSN -c \"
  SELECT COUNT(*) as total_facts FROM facts WHERE fact_class IN ('A', 'B');
\""
```

If `fact_class='B'` rows exist in `facts` table, promotion IS working.

### Step 6: Re-Embedder Logs

```bash
ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | grep 're_embedder' | tail -50"
```

Look for:
- `re_embedder.start` — service started
- `re_embedder.promotion_complete` — promotions are running
- `re_embedder.loop_error` — errors in main loop
- `ERROR` level entries — failures

---

## Fix Strategies

### If Re-Embedder Is Dead

1. **Check Docker logs for startup errors:**
   ```bash
   ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | grep -A 5 'Starting re_embedder'"
   ```

2. **Verify environment variables are set:**
   ```bash
   ssh docker-host -x "sudo docker inspect faultline-wgm | grep -E 'POSTGRES_DSN|QDRANT_URL|QWEN_API_URL'"
   ```

3. **Restart the container:**
   ```bash
   ssh docker-host -x "sudo docker restart faultline-wgm"
   sleep 5
   ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | tail -30"
   ```

### If Re-Embedder Is Running But Not Promoting

1. **Manually trigger a promotion cycle** (diagnostic):
   ```python
   # In src/re_embedder/embedder.py, add to main() loop:
   n_promoted = promote_staged_facts(db, qdrant_url)
   print(f"DEBUG: Promoted {n_promoted} facts")
   ```

2. **Check for fact_class='B' with confirmed_count >= 3:**
   - If they exist and aren't promoted, there's a transaction/commit bug
   - If they don't exist, confirmed_count is never reaching threshold

### If Facts Never See Duplicates

1. **Verify filter extraction is working:**
   ```bash
   ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | grep 'ingest\.' | head -10"
   ```

2. **Ensure same fact is being extracted from multiple messages:**
   - Test by sending the same fact twice in separate messages
   - Check `staged_facts` table for `confirmed_count` increment

---

## Code References

- **Re-embedder launcher:** `docker-entrypoint.sh:18–26`
- **Promotion logic:** `src/re_embedder/embedder.py:302–370`, called at `main()` line 852
- **Staged fact insertion:** `src/api/main.py:284–325` (`_commit_staged`)
- **Schema (confirmed_count default):** `migrations/012_staged_facts.sql:15`
- **Promotion query:** `src/re_embedder/embedder.py:316–323`

## Expected Behavior (Once Fixed)

1. User sends message with fact → `/ingest` called → staged fact inserted with `confirmed_count=0`
2. User sends another message with same fact → `/ingest` called → staged fact updated, `confirmed_count=1`
3. Two more identical facts → `confirmed_count` reaches 3
4. Re-embedder polls (`REEMBED_INTERVAL`=10s default) → detects eligible fact → calls `promote_staged_facts()`
5. Fact moves to `facts` table with `fact_class='B'`
6. Future `/query` calls return the fact from `facts` table (PostgreSQL authoritative)

---

## Related Issues

- **dBug-016:** OpenWebUI crash on missing chat_id (separate, but blocks ingest calls in main chat flow)
- **dBug-018:** Re-embedder Qdrant reconciliation (may cause collection/point issues)

---

## Testing

Once re-embedder is confirmed running:

1. Send 4 separate messages, each containing: "my name is alice"
2. Query: `SELECT count(*) FROM staged_facts WHERE rel_type='pref_name' AND object_id='alice'`
3. Expected `confirmed_count`: 3
4. Query: `SELECT count(*) FROM facts WHERE rel_type='pref_name' AND object_id='alice'`
5. Expected: 1 row (fact moved from staged to permanent)

---

## Root Cause Summary

**Confirmed:** Promotion mechanism exists in code (`promote_staged_facts()` at line 852 of main loop).

**Unknown:** Whether re-embedder is actually **running** in the deployed container.

**Next Step:** SSH to TrueNAS, verify re-embedder process exists, and check logs for startup/runtime errors. If it's dead, investigate PostgreSQL/Qdrant/API connectivity. If it's alive but not promoting, check if any Class B facts have `confirmed_count >= 3`.
