# dBug-?: Streaming + Connection Pool Architecture Flaw

**Status:** INVESTIGATION  
**Date Discovered:** 2026-05-19  
**Severity:** CRITICAL (Silent Request Duplication)  
**Component:** LLM Streaming + Database Connection Lifecycle

## Problem Statement

When an LLM stream is mid-processing and a database connection is dropped (or connection pool exhausted), the connection dies but **the HTTP request does NOT fail cleanly**. Instead:

1. LLM call is queued and **executes**
2. DB connection drops mid-stream
3. Request silently **retries** (reconnects) without cancelling the queued LLM call
4. **Duplicate** LLM execution happens
5. Queue backs up with cascading retries

**Observable symptom:** "Gen (+330 queued)" = 330 duplicate/retry requests, not 330 fresh ingest requests.

## Root Causes

### 1. Unsynchronized Lifecycle Management

**Current state:**
- LLM streaming and DB connection lifecycles are independent
- Stream interruption doesn't cancel the queued LLM call
- Retry logic doesn't check if the call already executed

**Problem:**
- Stream starts → DB connection assigned
- LLM call queued
- DB connection drops (pool exhausted)
- Stream receives connection error, but LLM call already in queue
- HTTP request retries with new DB connection
- LLM call executes anyway → duplicate work

### 2. No Request Idempotency / Deduplication

**Current state:**
- No idempotency keys on ingest requests
- No deduplication before LLM calls
- No tracking of in-flight LLM calls

**Problem:**
- Retried request looks identical to original
- System has no way to know it's a retry
- LLM gets called again for the same input

### 3. Connection Pool Exhaustion Cascade

**Current state:**
- PostgreSQL `max_connections` is default (96-100)
- FaultLine + re_embedder + filter all compete for connections
- Re-embedder loops continuously, holding connections
- No connection keep-alive during streaming

**Problem:**
- Pool fills up → new requests wait
- Stream timeout → request retries
- Retried request takes another connection
- Retry happens again → loop

## Best Practice Standards (2026)

### Streaming with Connection Reliability

Sources:
- [Streaming LLM Responses: Interactive LLM Applications (Medium, 2026)](https://medium.com/@vishal.agarwal.iitk/streaming-llm-responses-interactive-llm-applications-0a83c48a3c52)
- [How to Handle LLM Responses Like a Pro (DEV Community)](https://dev.to/abhinav__ap/from-waiting-to-streaming-how-to-handle-llm-responses-like-a-pro-especially-with-json-2lgh)
- [How We Handle LLM Provider Failover at Scale (LLMGateway)](https://llmgateway.io/blog/how-we-handle-llm-provider-failover)
- [Handling Timeouts and Retries in LLM Systems (DAS Root, 2026)](https://dasroot.net/posts/2026/02/handling-timeouts-retries-llm-systems/)
- [Is Resumable LLM Streaming Hard? (Stardrift Blog)](https://stardrift.ai/blog/streaming-resumptions)
- [How to Build LLM Streams That Survive Reconnects (Upstash Blog)](https://upstash.com/blog/resumable-llm-streams)

**Best practices:**
1. **Decouple generation from delivery** — separate LLM generation (queue) from client streaming (connection)
2. **Use persistent storage for chunks** — Redis/Postgres stores each LLM chunk as generated, independent of client connection
3. **Error events with retry semantics** — send error event to client if stream dies; client can regenerate
4. **Connection keep-alive during streams** — don't release DB connection until response completes
5. **Transparent retries before sending data** — if error occurs before any data sent to client, retry; if data already sent, error event
6. **Exponential backoff + jitter** — retries use backoff to avoid cascade loops

### Connection Pooling Best Practices

Sources:
- [How to Use Connection Pooling Effectively (OneUptime, 2026)](https://oneuptime.com/blog/post/2026-01-27-connection-pooling-effectively/view)
- [Connection Pooling Patterns (Medium)](https://medium.com/@artemkhrenov/connection-pooling-patterns-optimizing-database-connections-for-scalable-applications-159e78281389)
- [Implicit Connection Pooling (John Jones, Oracle)](https://cjones-oracle.medium.com/implicit-connection-pooling-when-connections-overload-your-database-3fe7c59acae2)
- [SQLAlchemy Connection Pooling Documentation](https://docs.sqlalchemy.org/en/14/core/pooling.html)
- [SQL Server Connection Pooling (Microsoft Learn)](https://learn.microsoft.com/en-us/dotnet/framework/data/adonet/sql-server-connection-pooling)

**Best practices:**
1. **Right-size pool per workload** — DB_POOL_SIZE should match max concurrent operations (not total connections)
2. **Enable keep-alive** — reuse connections across requests via TCP keep-alive
3. **Validate on return** — execute "SELECT 1" before returning connection to pool to detect stale connections
4. **Set POOL_BOUNDARY** — STATEMENT (release per statement) vs TRANSACTION (release per commit/rollback)
5. **Avoid autocommit with streaming** — autocommit prevents multi-round-trip operations (like LOBs or streaming)
6. **Monitor pool saturation** — track available connections; alert when < 20% available
7. **Respect isolation level** — reused connections inherit previous isolation; can cause unexpected deadlocks

### Idempotency & Request Deduplication

Sources:
- [How to Prevent Duplicate API Requests (OneUptime, 2026)](https://oneuptime.com/blog/post/2026-01-25-prevent-duplicate-api-requests-deduplication-go/view)
- [Idempotency in Distributed Systems (Alok)](https://aloknecessary.github.io/blogs/idempotency-distributed-systems/)
- [Idempotency in API aliceign (Level Up Coding)](https://blog.levelupcoding.com/p/idempotency-in-api-aliceign-clearly-explained)
- [Building Robust APIs Preventing Duplicate Operations (Leapcell)](https://leapcell.io/blog/building-robust-apis-preventing-duplicate-operations-with-idempotency)
- [Idempotency in APIs: Handling Duplicate Requests (Medium)](https://medium.com/@mohitmallick/idempotency-in-apis-handling-duplicate-requests-the-right-way-c35d108f98e0)
- [Request Deduplication (Manning API aliceign Patterns)](https://livebook.manning.com/book/api-aliceign-patterns/chapter-26/v-7/)
- [Idempotency, Circuit Breakers and REST APIs (Sketech News)](https://sketechnews.substack.com/p/idempotency-duplicate-requests)
- [Deduplication Strategies in Microservices (OneUptime, 2026)](https://oneuptime.com/blog/post/2026-01-30-microservices-deduplication-strategies/view)

**Best practices:**
1. **Idempotency keys** — client generates UUID, inclualice in Idempotency-Key header
2. **Server-side caching** — store (key → result) in Redis/Postgres with TTL (4-24 hours)
3. **Scope keys** — include user_id, tenant_id, api_version in cache key to prevent cross-user collisions
4. **Idempotent replays** — if cache hit, return stored result + X-Idempotent-Replay: true header
5. **Deduplication ≠ idempotency** — deduplication prevents execution once; idempotency ensures same outcome
6. **Exactly-once semantics** — combine idempotency keys with transactional writes (one DB write per key)

## Current FaultLine State

### PostgreSQL Connection Pool

```
Current: DB_POOL_SIZE=50 (docker-compose.yml default)
Observed: "too many clients already" errors at peak load
Concurrent requesters: Filter + /ingest + /query + re_embedder loops
```

### LLM Streaming Integration

```
Current:
- /ingest calls LLM extraction synchronously
- Correction pipeline calls LLM for pattern extraction
- No idempotency keys on requests
- No deduplication before queuing

Problem:
- Connection drop → implicit retry
- Retry doesn't cancel original LLM call
- Same extraction runs 2+ times
```

### Request Lifecycle

```
Current:
1. Filter receives OpenWebUI message
2. Filter calls /ingest with text
3. /ingest extracts entities (LLM call)
4. If DB connection drops: connection error
5. Filter doesn't receive response
6. Filter retries /ingest (same request)
7. New /ingest processes, LLM extraction queued AGAIN
8. Original extraction STILL in queue

Result: 2 LLM calls for 1 user message, queue backs up
```

## Recommended Fixes (Priority Order)

### Phase 1: Immediate (Connection Exhaustion Stop-Gap)

1. Increase PostgreSQL `max_connections` to 200 (temporary relief)
2. Reduce DB_POOL_SIZE to actual concurrent limit (estimate: 10-15)
3. Add connection timeout + circuit breaker: fail fast instead of hanging
4. Monitor pool saturation: log when < 20% available

### Phase 2: Idempotency (Prevent Duplicate Execution)

1. Add Idempotency-Key header requirement to /ingest endpoint
2. Store request cache in Redis: (key → ingest_response) with 24h TTL
3. Before LLM call, check if key already cached
4. Return cached result + X-Idempotent-Replay: true header
5. Scope keys: `{user_id}:{text_hash}:{is_correction}` to prevent cross-user collisions

### Phase 3: Decoupled Streaming (Architectural)

1. Move LLM calls to job queue (Celery/RQ) with unique job IDs
2. Decouple generation (queue) from delivery (client connection)
3. Store LLM responses in persistent storage (Postgres) indexed by job_id
4. Client stream reads from persistent storage, not directly from queue
5. If connection drops, client can reconnect and resume from saved position

### Phase 4: Connection Keep-Alive (Persistence)

1. Extend DB connection lease for duration of request processing
2. Mark connection in-use until response sent to client (not before)
3. Implement statement-level connection release (POOL_BOUNDARY=STATEMENT) for concurrent requests
4. Test with load: verify no connection starvation under 100+ concurrent requests

## Testing Strategy

1. **Reproduction:** Simulate connection pool exhaustion (set max_connections=10, send 20 concurrent /ingest requests)
2. **Verify idempotency:** Send same request 3x with same key, confirm only 1 LLM call
3. **Verify keep-alive:** Monitor connection count while streaming large responses
4. **Verify deduplication:** Check LLM queue length under retry load (should be < 5% of request count)

## Open Questions

1. What is actual peak concurrent request count to FaultLine?
2. Are connection leaks present (connections not returned to pool)?
3. How long do LLM calls take on average? (impacts pool size calculation)
4. Does OpenWebUI have built-in idempotency support we can leverage?
