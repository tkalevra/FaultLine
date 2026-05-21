# dBug-057: Connection Leak — Idle-in-Transaction Pool Exhaustion

**Status:** CRITICAL  
**Discovered:** 2026-05-19 12:58  
**Symptoms:** 52 connections stuck in `idle in transaction` state alicepite pool_size=15  
**Impact:** Database connection pool exhaustion; prevents new requests after sustained load

---

## Observed Behavior

After Phase 2 (idempotency + deduplication) implementation and correction pipeline testing:

```
SELECT state, COUNT(*) FROM pg_stat_activity WHERE datname='faultline_test' GROUP BY state;

        state        | count 
---------------------+-------
 active              |     1
 idle                |    18
 idle in transaction |    52   ← LEAK
(3 rows)
```

- **Pool size:** 15
- **Idle-in-transaction:** 52 (3.5x pool capacity)
- **Impact:** New requests wait for transaction cleanup; HTTP timeouts escalate

## Root Cause

Transactions are opened in `/ingest`, `/extract/rewrite`, or correction pipeline but **not properly closed on error paths**:

1. `db = psycopg2.connect(...)` opens a connection
2. Code path diverges (correction applied, extraction failed, validation error, etc.)
3. One or more branches skip `db.commit()` or `db.rollback()`
4. Connection remains in transaction state indefinitely
5. Pool never reclaims it (idle-in-transaction = waiting for explicit cleanup)

## Affected Code Paths

Likely culprits in `/ingest` (lines ~4907-6692):

- Line 4907: `db = psycopg2.connect(...)` early connection
- Lines 4950-4995: Retraction detection branch (no cleanup on exception)
- Lines 5000-5070: Correction pipeline (error path at line 5069 has `db.rollback()` but not all branches)
- Lines 5073-5232: Extraction via `/extract/rewrite` (error handling at 5230-5232)
- Lines 5936-5980: Scalar storage (exception at 5978 logs warning but may not rollback cleanly)
- Lines 6683-6692: Final cleanup in `finally` block (only closes, doesn't rollback)

## Required Fix

**Pattern:** Every `try` block that touches `db` must have explicit `finally` cleanup:

```python
try:
    # ... operations ...
    db.commit()
except Exception as e:
    log.error("...", error=str(e))
    db.rollback()  # ← REQUIRED on exception
finally:
    db.close()  # ← Closes connection, but only after explicit rollback
```

**Current pattern (BROKEN):**
```python
finally: db.close()  # ← Close without rollback leaves transaction open if exception occurred
```

The `db.close()` in a `finally` block after an exception does NOT automatically rollback an open transaction. The connection is returned to the pool still holding the transaction, blocking other clients.

## Verification

After fix, monitor:
```bash
ssh docker-host -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c \"SELECT state, COUNT(*) FROM pg_stat_activity WHERE datname='faultline_test' GROUP BY state;\""
```

Expected post-fix:
```
        state        | count 
---------------------+-------
 active              |     1
 idle                |    ~5  (only active request + minor lingering)
 idle in transaction |     0  (zero leak)
(3 rows)
```

## Files to Review

1. `src/api/main.py:4907-6692` — Primary ingest endpoint (largest transaction scope)
2. `src/api/main.py:3190-3250` — `/extract/rewrite` endpoint
3. `src/wgm/gate.py` — Any `db.cursor()` blocks with exception handling
4. `src/re_embedder/embedder.py` — Poll loop DB operations

## Related Issues

- [[dBug-050]] — Request duplication from connection pool backpressure
- [[dBug-051]] — LLM prompt bloat causing queue backup
- [[dprompt-116]] — Pattern-driven context filtering (attempted mitigation, but doesn't fix connection leak)

---

## Timeline

- **2026-05-14**: Connection exhaustion first observed (69 active with pool_size=15)
- **2026-05-18**: Phase 2 idempotency implemented; connection pressure persisted
- **2026-05-19 12:58**: Connection leak diagnosed: 52 idle-in-transaction connections

The leak accumulates under sustained load (multiple corrections, extractions), draining the pool and causing cascading 504 timeouts on incoming requests.
