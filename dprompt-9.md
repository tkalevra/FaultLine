# Deepseek Validation Prompt: Temporal Events End-to-End (curl + SQL + logs)

**Scope:** Validate temporal events work end-to-end: extraction → storage → retrieval → memory injection. Use curl, SQL, and Docker logs.

**Why:** Phase 7 code is live. Need definitive proof temporal flow works before moving to systematic test coverage.

---

## Environment

- Docker running: `docker compose up` (FaultLine API on port 8001)
- PostgreSQL accessible: `psql -U faultline -d faultline_test`
- Logs accessible: `docker logs faultline-api` and `docker logs faultline-postgres`
- Test user: `test_temporal_123`

---

## Test Flow

### Step 1: Extract Birthday (curl + SQL)

```bash
# POST /ingest: "I was born on May 3rd, 1990"
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I was born on May 3rd, 1990",
    "user_id": "test_temporal_123"
  }'

# Expected response:
# { "status": "success", "facts_committed": 1, ... }
```

**Then validate SQL:**

```sql
-- Check events table
SELECT * FROM events 
WHERE user_id = 'test_temporal_123' 
  AND event_type = 'born_on';

-- Expected:
-- id | user_id | subject_id | event_type | occurs_on | recurrence | confidence | created_at
-- 1  | test... | user       | born_on    | may 3rd... | yearly     | 0.9        | [now]
```

**Check logs:**

```bash
docker logs faultline-api | grep -i "born_on\|temporal\|event"
# Should show: routing to events table, insertion success
```

---

### Step 2: Extract Appointment (curl + SQL)

```bash
# POST /ingest: "I have a dentist appointment on July 16, 2026"
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I have a dentist appointment on July 16, 2026",
    "user_id": "test_temporal_123"
  }'
```

**SQL validation:**

```sql
SELECT * FROM events 
WHERE user_id = 'test_temporal_123' 
  AND event_type = 'appointment_on';

-- Expected:
-- event_type: appointment_on, occurs_on: july 16, 2026, recurrence: once
```

---

### Step 3: Extract Other's Birthday (curl + SQL)

```bash
# POST /ingest: "My wife Marla was born on June 15, 1992"
curl -X POST http://localhost:8001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My wife Marla was born on June 15, 1992",
    "user_id": "test_temporal_123"
  }'
```

**SQL validation:**

```sql
-- Should create two things:
-- 1. Fact: (user, spouse, marla_uuid)
SELECT * FROM facts 
WHERE user_id = 'test_temporal_123' 
  AND rel_type = 'spouse';

-- 2. Event: (marla_uuid, born_on, "june 15, 1992")
SELECT * FROM events 
WHERE user_id = 'test_temporal_123' 
  AND event_type = 'born_on' 
  AND occurs_on = 'june 15, 1992';

-- Expected:
-- Fact entry + Event entry (two rows)
```

---

### Step 4: Query and Merge (curl)

```bash
# POST /query: "When was I born?"
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "When was I born?",
    "user_id": "test_temporal_123"
  }'

# Expected response includes:
# {
#   "facts": [
#     {
#       "subject": "user",
#       "object": "may 3rd, 1990",
#       "rel_type": "born_on",
#       "source": "events_table",
#       "recurrence": "yearly"
#     },
#     ...
#   ]
# }
```

**Validate events merged:**

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Tell me about my wife",
    "user_id": "test_temporal_123"
  }' | jq '.facts[] | select(.rel_type == "born_on" or .rel_type == "spouse")'

# Should return both:
# - spouse: marla
# - born_on for marla: june 15, 1992
```

---

### Step 5: Memory Injection (curl + logs)

```bash
# Simulate OpenWebUI inlet with /query + memory injection
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How old is my wife?",
    "user_id": "test_temporal_123"
  }' | jq '.facts'
```

**Check Filter logs:**

```bash
docker logs faultline-api | grep -i "⭐\|📅\|memory\|event"
# Should show formatted events in memory block
```

---

## Edge Cases to Validate

1. **Fuzzy date:**
   ```bash
   curl -X POST http://localhost:8001/ingest \
     -H "Content-Type: application/json" \
     -d '{
       "text": "I was born sometime in 1990",
       "user_id": "test_temporal_123"
     }'
   ```
   SQL: Check `confidence < 1.0` for fuzzy dates

2. **Correction:**
   ```bash
   # Turn 1: "I was born May 3rd"
   curl ... -d '{"text": "I was born May 3rd", "user_id": "test_temporal_123"}'
   
   # Turn 2: "Actually June 3rd"
   curl ... -d '{"text": "Actually born June 3rd, not May 3rd", "user_id": "test_temporal_123"}'
   ```
   SQL: Check events table has only ONE row for born_on (updated, not duplicate)
   ```sql
   SELECT COUNT(*) FROM events 
   WHERE user_id = 'test_temporal_123' 
     AND event_type = 'born_on' 
     AND occurs_on = 'june 3rd';
   -- Expected: 1 (correction overwrote)
   ```

3. **Multiple events:**
   ```bash
   curl ... -d '{"text": "My birthday is May 3rd and our anniversary is June 20th", "user_id": "test_temporal_123"}'
   ```
   SQL: Check both events in table
   ```sql
   SELECT event_type, occurs_on FROM events 
   WHERE user_id = 'test_temporal_123';
   -- Expected: born_on + anniversary_on (2 rows)
   ```

4. **No relative dates:**
   ```bash
   curl ... -d '{"text": "I am going on vacation next week", "user_id": "test_temporal_123"}'
   ```
   SQL: Check NO events created
   ```sql
   SELECT COUNT(*) FROM events 
   WHERE user_id = 'test_temporal_123' 
     AND occurs_on LIKE '%next%';
   -- Expected: 0
   ```

---

## Checklist

### Extraction
- ✅ Birthday patterns extract to events table
- ✅ Anniversaries extract with recurrence="yearly"
- ✅ Appointments extract with recurrence="once"
- ✅ Other entities' birthdays routed correctly
- ✅ Compound patterns (age + date) create both fact + event
- ✅ Corrections UPDATE existing event (no duplicates)
- ✅ Fuzzy dates marked low_confidence
- ✅ No relative dates extracted

### Storage
- ✅ Events table has correct schema (occurs_on, recurrence, confidence)
- ✅ UNIQUE constraint works (no duplicate events per entity/type)
- ✅ Confidence values reflect extraction quality
- ✅ Recurrence field populated correctly

### Query
- ✅ `/query` merges events from events table
- ✅ Events appear in facts response with source="events_table"
- ✅ Preferred display names applied to event subjects
- ✅ Recurrence metadata included

### Memory
- ✅ Filter formats yearly events as "⭐ X's birthday: date (annually)"
- ✅ Filter formats one-time events as "📅 X date: date"
- ✅ Events inject naturally into memory block

### Logs
- ✅ No errors in Docker logs
- ✅ Event routing visible in logs
- ✅ Query merge visible in logs
- ✅ Memory injection visible in logs

---

## Done When

All checks pass:
- ✅ 5 basic curl tests succeed (birthday, appointment, spouse+birthday, query, memory)
- ✅ SQL validates storage (events table has correct rows)
- ✅ 4 edge cases pass (fuzzy, correction, multiple, no-relative)
- ✅ Logs show clean execution (no errors)
- ✅ Fact count and formatting correct in memory injection

---

## Report Back

Return:
1. Summary table: test | status | details
2. Any errors from curl/SQL/logs
3. Screenshot of events table (full row for one event)
4. Log snippet showing memory injection
5. Any regressions on non-temporal facts

Ship it.
