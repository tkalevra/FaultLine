# FaultLine — Next Steps

**Status as of 2026-04-30** | Filter v1.3.0. Dual-path query (PostgreSQL baseline + graph traversal + Qdrant). System message injection. All 38 unit + integration tests passing.

---

## Current State

| Capability | Status |
|---|---|
| Fact extraction (GLiNER2 + Qwen rewrite) | ✅ Operational |
| PostgreSQL persistence with ontology validation | ✅ Operational |
| Qdrant re-embedder (background sync) | ✅ Operational |
| `/query` dual-path: baseline facts + graph traversal + Qdrant | ✅ Operational |
| OpenWebUI filter — system message injection (no relevance gate) | ✅ Operational |
| Per-user Qdrant collections | ✅ Operational |
| Short-term → long-term memory promotion (confidence scoring) | ✅ Operational |

---

## Priority 1 — Temporal Awareness

Facts exist in time. Without date context the model can't reason about staleness, countdowns, or when something happened.

### 1a — Date storage on facts

Add `valid_from` and `valid_until` columns to the `facts` table (nullable `TIMESTAMPTZ`). Migration needed.

```sql
ALTER TABLE facts ADD COLUMN valid_from  TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE facts ADD COLUMN valid_until TIMESTAMPTZ DEFAULT NULL;
```

Qwen extraction prompt needs rules for temporal assertions:
- "Chris's birthday is March 12" → `born_on` triple + `valid_from = null` (static, always true)
- "Jordan is visiting next Thursday" → `lives_at / visiting` triple + `valid_until = <date>`
- "The concert is May 5th" → event triple + `valid_from = 2026-05-05`

### 1b — Temporal classification in /ingest

When a fact is committed, classify it as one of:
- **static** — true indefinitely (birthdate, name, relationship)
- **dated** — has a known point-in-time (was born, met someone on date X)
- **ephemeral** — time-bounded and expires (event, visit, appointment)

Store this as a `temporal_class` column: `('static', 'dated', 'ephemeral')`.

### 1c — Countdown and elapsed rendering in the filter

When the filter builds the memory block, inject time context for dated/ephemeral facts:

```
- Jordan is visiting on 2026-05-10 (10 days from now)
- Chris's birthday was 2026-03-12 (49 days ago)
- Concert on 2026-04-20 (10 days ago — likely past)
```

This requires the filter to know today's date (trivial) and compare it against `valid_from` / `valid_until`.

---

## Priority 2 — Memory Regression Gate (Staleness → Archive)

Facts degrade. A `lives_at` from 3 years ago is weaker than one from last week. The system needs a principled way to:
1. Reduce confidence on facts that haven't been reinforced
2. Mark facts as **stale** before fully superseding them
3. Archive (soft-delete from Qdrant, retain in PostgreSQL) facts that have fallen below a threshold

### 2a — Staleness scoring in re_embedder

The re_embedder already runs `promote_facts()` to increase confidence on confirmed facts. Add a symmetric `decay_facts()`:

```python
def decay_facts(db_conn, decay_rate: float = 0.05) -> None:
    """Reduce confidence on facts not seen or confirmed recently."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE facts
            SET confidence = GREATEST(confidence - %s, 0.0)
            WHERE superseded_at IS NULL
            AND confirmed_count = 0
            AND last_seen_at < now() - interval '30 days'
            AND temporal_class != 'static'
            AND confidence > 0.0
            """,
            (decay_rate,)
        )
```

Static facts (birthdate, name, relationships) are exempt from decay.

### 2b — Archive threshold

Facts that fall below a configurable `ARCHIVE_CONFIDENCE_THRESHOLD` (default: `0.2`) should be:
1. Removed from Qdrant (so they stop surfacing in queries) — re_embedder deletion pass handles this via `superseded_at`
2. Retained in PostgreSQL with `archived_at` timestamp for audit / recovery

Add `archived_at TIMESTAMPTZ DEFAULT NULL` to the facts table. The re_embedder sets this when confidence drops below threshold.

### 2c — Archive threshold as environment variable

```
ARCHIVE_CONFIDENCE_THRESHOLD=0.2    # below this, remove from Qdrant, keep in PG
DECAY_INTERVAL_DAYS=30              # days without reinforcement before decay starts
DECAY_RATE=0.05                     # confidence reduction per decay cycle
```

---

## Priority 3 — Dual Search Path Verification

The `/query` endpoint currently runs three sources and merges them:

| Source | When | What |
|---|---|---|
| PostgreSQL baseline | Always (if canonical identity known) | `lives_at`, `age`, `height`, `weight`, `works_for`, etc. |
| PostgreSQL graph traversal | When query matches self-ref signals | All facts anchored to identity + 2-hop related entities |
| Qdrant vector search | Always | Cosine similarity ≥ 0.3, limit 10 |

**Verification needed**: confirm that for a cold-start user (no graph traversal triggered, no Qdrant hits above threshold), the baseline path still returns location facts correctly. Test with "What's the weather tomorrow?" against a known `lives_at` fact.

**Potential gap**: the `_SELF_REF_SIGNALS` set in `/query` is checked against `query_lower` (the full query text lowercased). Signals like "my home" and "where do i live" are present. Consider whether conversational fragments like "near me" or "around here" should trigger graph traversal.

---

## Backlog

| Item | Notes |
|---|---|
| `faultline_function.py` parity | The explicit Tool function doesn't have the same baseline-fetch fix as the filter. Parity pass needed. |
| Qdrant score threshold tuning | 0.3 may be too high for short or sparse queries. Consider adaptive threshold based on result count. |
| Fact confidence UI | No way to see or manage individual fact confidence scores from OpenWebUI. |
| Multi-user isolation audit | Per-user Qdrant collections are implemented; verify no cross-user fact leakage in the merge paths. |
| Test coverage for `/query` baseline path | No unit tests for the new baseline fact fetch added in the dual-path refactor. |
